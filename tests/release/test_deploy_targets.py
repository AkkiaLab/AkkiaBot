"""Where a release is deployed comes from machine-local configuration, never from the code."""

import subprocess
from pathlib import Path

import pytest

from scripts.release import cli, deploy
from scripts.release.versions import ReleaseError
from tests.release.conftest import EXAMPLE_TARGETS, example_target

PRIVATE = "do-not-echo-this-value"


def test_targets_come_from_the_environment() -> None:
    production = example_target("production")
    assert production.ssh == "prod-host.example.invalid"
    assert production.directory == "~/stack-production"
    assert production.health_url == deploy.ENVIRONMENTS["production"].health_url
    assert production.template is None
    staging = example_target("staging")
    assert staging.ssh is None  # explicit "local": this machine
    assert staging.directory == "/srv/akkiabot-staging"
    assert staging.template == "staging/app.env.tpl"
    assert deploy.host_shell(production, "true") == [
        "ssh",
        "--",
        "prod-host.example.invalid",
        "cd -- ~/stack-production && true",
    ]
    assert deploy.host_shell(staging, "true") == [
        "bash",
        "-c",
        "cd -- /srv/akkiabot-staging && true",
    ]


@pytest.mark.parametrize("name", sorted(deploy.ENVIRONMENTS))
def test_missing_configuration_is_an_error(name: str, tmp_path: Path) -> None:
    missing = tmp_path / "absent.env"
    ssh_key, dir_key = deploy.target_keys(name)
    with pytest.raises(ReleaseError) as exc:
        deploy.resolve_target(name, environ={}, targets_file=missing)
    message = str(exc.value)
    assert dir_key in message
    assert ssh_key in message
    assert str(missing) in message
    # Each half alone is refused: no default directory, and a missing ssh host is not
    # "this machine" (that has to be written as `local`).
    with pytest.raises(ReleaseError, match=dir_key):
        deploy.resolve_target(name, environ={ssh_key: "host"}, targets_file=missing)
    with pytest.raises(ReleaseError, match=ssh_key) as only_dir:
        deploy.resolve_target(name, environ={dir_key: "/srv/x"}, targets_file=missing)
    assert dir_key not in str(only_dir.value).split("缺少")[1].split("（")[0]
    with pytest.raises(ReleaseError, match=ssh_key):
        deploy.resolve_target(name, environ={dir_key: "/srv/x", ssh_key: ""}, targets_file=missing)


def test_targets_file_and_precedence(tmp_path: Path) -> None:
    path = tmp_path / "release-targets.env"
    path.write_text(
        "# machine-local, never committed\n"
        "\n"
        "AKKIABOT_PRODUCTION_SSH='file-host'\n"
        'export AKKIABOT_PRODUCTION_DIR="/srv/from-file"\n',
        encoding="utf-8",
    )
    from_file = deploy.resolve_target("production", environ={}, targets_file=path)
    assert (from_file.ssh, from_file.directory) == ("file-host", "/srv/from-file")
    # Process environment beats the file, the command line beats both.
    env = {"AKKIABOT_PRODUCTION_SSH": "env-host"}
    mixed = deploy.resolve_target("production", environ=env, targets_file=path)
    assert (mixed.ssh, mixed.directory) == ("env-host", "/srv/from-file")
    cli_set = deploy.resolve_target(
        "production", ssh="local", directory="/srv/cli", environ=env, targets_file=path
    )
    assert (cli_set.ssh, cli_set.directory) == (None, "/srv/cli")


def test_default_targets_file_location(tmp_path: Path) -> None:
    assert deploy.default_targets_file({"XDG_CONFIG_HOME": str(tmp_path)}) == (
        tmp_path / "akkiabot" / "release-targets.env"
    )
    custom = tmp_path / "x.env"
    assert deploy.default_targets_file({deploy.TARGETS_FILE_ENV: str(custom)}) == custom


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("AKKIABOT_PRODUCTION_SSH", "-oProxyCommand=" + PRIVATE, "不能以 - 开头"),
        ("AKKIABOT_PRODUCTION_SSH", PRIVATE + " host", "ssh 主机"),
        ("AKKIABOT_PRODUCTION_SSH", PRIVATE + ";id", "ssh 主机"),
        ("AKKIABOT_PRODUCTION_DIR", "relative/" + PRIVATE, "绝对路径"),
        ("AKKIABOT_PRODUCTION_DIR", "~root/" + PRIVATE, "绝对路径"),
        ("AKKIABOT_PRODUCTION_DIR", "/srv/" + PRIVATE + "\n/x", "控制字符"),
    ],
)
def test_invalid_values_are_rejected_without_echo(key: str, value: str, reason: str) -> None:
    environ = {**EXAMPLE_TARGETS, key: value}
    with pytest.raises(ReleaseError, match=reason) as exc:
        deploy.resolve_target("production", environ=environ)
    assert key in str(exc.value)
    assert PRIVATE not in str(exc.value)


def test_malformed_file_and_unknown_environment(tmp_path: Path) -> None:
    path = tmp_path / "bad.env"
    path.write_text(f"AKKIABOT_STAGING_DIR {PRIVATE}\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="第 1 行") as exc:
        deploy.resolve_target("staging", environ={}, targets_file=path)
    assert PRIVATE not in str(exc.value)
    with pytest.raises(ReleaseError, match="未知环境"):
        deploy.resolve_target("qa", environ=EXAMPLE_TARGETS)


@pytest.mark.parametrize(
    "directory",
    ["/srv/a b/$(touch pwned)", "/srv/`touch pwned`;x", "~/it's here/$HOME", "~/"],
)
def test_directory_reaches_the_shell_literally(directory: str, tmp_path: Path) -> None:
    """The directory is one literal word for the host shell; only a leading ~/ expands."""
    home = tmp_path / "home"
    home.mkdir()
    target = deploy.resolve_target(
        "staging", environ={"AKKIABOT_STAGING_SSH": "local", "AKKIABOT_STAGING_DIR": directory}
    )
    argv = deploy.host_shell(target, 'printf %s "$PWD"')
    expected = Path(directory.replace("~", str(home), 1)) if directory.startswith("~") else None
    if expected is None:
        # Absolute paths: run inside a scratch root so the literal path can exist.
        literal = tmp_path / "root" / directory.lstrip("/")
        literal.mkdir(parents=True)
        argv = deploy.host_shell(
            deploy.resolve_target(
                "staging",
                environ={"AKKIABOT_STAGING_SSH": "local", "AKKIABOT_STAGING_DIR": str(literal)},
            ),
            'printf %s "$PWD"',
        )
        expected = literal
    else:
        expected.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        argv, capture_output=True, text=True, check=True, cwd=tmp_path, env={"HOME": str(home)}
    )
    assert Path(proc.stdout) == expected
    assert not (tmp_path / "pwned").exists()


def test_cli_refuses_partial_configuration_before_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Directory without ssh host: no fallback to this machine, nothing locked or deployed."""
    monkeypatch.setattr(deploy, "environment_lock", lambda name: pytest.fail("lock taken"))
    monkeypatch.setattr(deploy, "deploy", lambda *a, **k: pytest.fail("deploy ran"))
    monkeypatch.setenv("AKKIABOT_PRODUCTION_DIR", "/srv/stack-production")
    args = cli.build_parser().parse_args(["deploy", "production", "v1.0.0"])
    with pytest.raises(ReleaseError, match="AKKIABOT_PRODUCTION_SSH"):
        cli.cmd_deploy(None, None, args)  # type: ignore[arg-type]
    assert cli.main(["deploy", "production", "v1.0.0"]) == 3
    monkeypatch.delenv("AKKIABOT_PRODUCTION_DIR")
    assert cli.main(["deploy", "production", "v1.0.0"]) == 3

"""Where a release is deployed comes from machine-local configuration, never from the code."""

from pathlib import Path

import pytest

from scripts.release import cli, deploy
from scripts.release.versions import ReleaseError
from tests.release.conftest import EXAMPLE_TARGETS, example_target


def test_targets_come_from_the_environment() -> None:
    production = example_target("production")
    assert production.ssh == "prod-host.example.invalid"
    assert production.directory == "~/stack-production"
    assert production.health_url == deploy.ENVIRONMENTS["production"].health_url
    assert production.template is None
    staging = example_target("staging")
    assert staging.ssh is None  # empty ssh: this machine
    assert staging.directory == "/srv/akkiabot-staging"
    assert staging.template == "staging/app.env.tpl"
    assert deploy.host_shell(production, "true") == [
        "ssh",
        "prod-host.example.invalid",
        "cd ~/stack-production && true",
    ]
    assert deploy.host_shell(staging, "true") == ["bash", "-c", "cd /srv/akkiabot-staging && true"]


@pytest.mark.parametrize("name", sorted(deploy.ENVIRONMENTS))
def test_missing_configuration_is_an_error(name: str, tmp_path: Path) -> None:
    missing = tmp_path / "absent.env"
    with pytest.raises(ReleaseError) as exc:
        deploy.resolve_target(name, environ={}, targets_file=missing)
    message = str(exc.value)
    ssh_key, dir_key = deploy.target_keys(name)
    assert dir_key in message
    assert ssh_key in message
    assert str(missing) in message
    # An ssh host alone is not enough: there is no default directory to fall back to.
    with pytest.raises(ReleaseError, match=dir_key):
        deploy.resolve_target(name, environ={ssh_key: "host"}, targets_file=missing)


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
        "production", ssh="", directory="/srv/cli", environ=env, targets_file=path
    )
    assert (cli_set.ssh, cli_set.directory) == (None, "/srv/cli")


def test_default_targets_file_location(tmp_path: Path) -> None:
    assert deploy.default_targets_file({"XDG_CONFIG_HOME": str(tmp_path)}) == (
        tmp_path / "akkiabot" / "release-targets.env"
    )
    custom = tmp_path / "x.env"
    assert deploy.default_targets_file({deploy.TARGETS_FILE_ENV: str(custom)}) == custom


def test_malformed_values_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.env"
    path.write_text("AKKIABOT_STAGING_DIR\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="第 1 行"):
        deploy.resolve_target("staging", environ={}, targets_file=path)
    with pytest.raises(ReleaseError, match="空白"):
        deploy.resolve_target("staging", environ={"AKKIABOT_STAGING_DIR": "/srv/a b"})
    with pytest.raises(ReleaseError, match="未知环境"):
        deploy.resolve_target("qa", environ=EXAMPLE_TARGETS)


def test_cli_refuses_to_deploy_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """No built-in host: the command stops before any lock, ssh or GitHub call."""
    monkeypatch.setattr(deploy, "environment_lock", lambda name: pytest.fail("lock taken"))
    monkeypatch.setattr(deploy, "deploy", lambda *a, **k: pytest.fail("deploy ran"))
    args = cli.build_parser().parse_args(["deploy", "production", "v1.0.0"])
    with pytest.raises(ReleaseError, match="AKKIABOT_PRODUCTION_DIR"):
        cli.cmd_deploy(None, None, args)  # type: ignore[arg-type]
    assert cli.main(["deploy", "production", "v1.0.0"]) == 3

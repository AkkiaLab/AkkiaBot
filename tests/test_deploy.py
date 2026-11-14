"""Guards for the production compose stack (deploy/) and its runbook (docs/operations.md).

Static checks run everywhere; the ``docker compose config`` check runs when the
compose CLI is installed (it needs no daemon). The backup script is exercised
against a fake ``docker`` executable.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from akkiabot_core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
COMPOSE_FILE = DEPLOY / "compose.yaml"
OPERATIONS = REPO_ROOT / "docs" / "operations.md"

APP_PORT = 8000
METRICS_PORT = 9464
#: Variables compose.yaml sets for the app itself; app.env must not need them.
STACK_WIRED = {
    "DB_URL",
    "REDIS_URL",
    "SERVER_HOST",
    "SERVER_PORT",
    "OBSERVABILITY_LOG_FORMAT",
    "OBSERVABILITY_METRICS_HOST",
    "OBSERVABILITY_METRICS_PORT",
}
#: Settings that hold credentials: templates may only reference them via op://.
SECRET_KEYS = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "LLM_API_KEY",
    "WHOIS_IPINFO_TOKEN",
    "TURNSTILE_SECRET_KEY",
    "OBSERVABILITY_SENTRY_DSN",
}


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _services() -> dict[str, Any]:
    return _compose()["services"]


def _env_keys(path: Path) -> dict[str, str]:
    """``KEY=value`` lines of an env file (comments and blanks skipped)."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f"{path.name}: not KEY=value: {key}"
        out[key] = value
    return out


def _settings_env_names() -> set[str]:
    names: set[str] = set()
    for group, field in Settings.model_fields.items():
        model = field.annotation
        assert isinstance(model, type)
        assert issubclass(model, BaseModel)
        names.update(f"{group}_{name}".upper() for name in model.model_fields)
    return names


# ------------------------------------------------------------------ compose


def test_stack_services() -> None:
    assert set(_services()) == {"app", "migrate", "postgres", "redis", "cloudflared"}


def test_migrate_runs_before_app() -> None:
    services = _services()
    migrate, app = services["migrate"], services["app"]
    assert migrate["command"] == ["migrate"]
    assert migrate["restart"] == "no"
    assert migrate["image"] == app["image"]
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert app["command"] == ["serve"]
    assert app["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


def test_only_app_http_port_is_published_on_loopback() -> None:
    for name, service in _services().items():
        ports = service.get("ports", [])
        if name != "app":
            assert not ports, f"{name} must not publish ports"
            continue
        assert len(ports) == 1
        host, _, container = str(ports[0]).rpartition(":")
        assert host.startswith("127.0.0.1:"), "app port must bind loopback only"
        assert int(container) == APP_PORT
        assert str(METRICS_PORT) not in str(ports[0])


def test_metrics_listener_is_internal() -> None:
    env = _services()["app"]["environment"]
    assert env["OBSERVABILITY_METRICS_PORT"] == str(METRICS_PORT)
    assert int(env["SERVER_PORT"]) == APP_PORT
    assert str(METRICS_PORT) != env["SERVER_PORT"]


def test_tunnel_only_shares_edge_network_with_app() -> None:
    compose = _compose()
    services = compose["services"]
    assert compose["networks"]["backend"]["internal"] is True
    assert services["cloudflared"]["networks"] == ["edge"]
    on_edge = {name for name, s in services.items() if "edge" in s.get("networks", [])}
    assert on_edge == {"app", "cloudflared"}
    for name in ("postgres", "redis", "migrate"):
        assert services[name]["networks"] == ["backend"]


def test_healthchecks_present() -> None:
    services = _services()
    for name in ("app", "postgres", "redis", "cloudflared"):
        assert "healthcheck" in services[name], name
    assert "/readyz" in " ".join(services["app"]["healthcheck"]["test"])
    assert services["cloudflared"]["depends_on"]["app"]["condition"] == "service_healthy"


def test_data_lives_in_named_volumes() -> None:
    compose = _compose()
    assert set(compose["volumes"]) == {"postgres-data", "redis-data"}
    assert "postgres-data:/var/lib/postgresql/data" in compose["services"]["postgres"]["volumes"]


def test_stack_wired_keys_match_compose() -> None:
    env = _services()["app"]["environment"]
    assert set(env) == STACK_WIRED
    assert _settings_env_names() >= STACK_WIRED


# ------------------------------------------------------------ env examples


@pytest.mark.parametrize("path", [DEPLOY / ".env.example", DEPLOY / "staging" / ".env.example"])
def test_compose_variables_documented(path: Path) -> None:
    used = set(re.findall(r"\$\{([A-Z_]+)", COMPOSE_FILE.read_text(encoding="utf-8")))
    keys = _env_keys(path)
    assert used <= set(keys), f"{path.name} misses {sorted(used - set(keys))}"
    # Secrets are left for the operator to fill in.
    assert keys["POSTGRES_PASSWORD"] == ""
    assert keys["CLOUDFLARED_TOKEN"] == ""


def test_staging_stack_does_not_collide_with_production() -> None:
    prod = _env_keys(DEPLOY / ".env.example")
    staging = _env_keys(DEPLOY / "staging" / ".env.example")
    assert prod["COMPOSE_PROJECT_NAME"] != staging["COMPOSE_PROJECT_NAME"]
    assert prod["AKKIABOT_HTTP_PORT"] != staging["AKKIABOT_HTTP_PORT"]


@pytest.mark.parametrize("path", [DEPLOY / "app.env.example", DEPLOY / "staging" / "app.env.tpl"])
def test_app_env_keys_are_settings(path: Path) -> None:
    keys = set(_env_keys(path))
    assert keys <= _settings_env_names(), f"unknown keys: {sorted(keys - _settings_env_names())}"
    assert not keys & STACK_WIRED, "set by compose.yaml, drop from the app env file"
    assert "TELEGRAM_BOT_TOKEN" in keys


def test_production_example_leaves_secrets_empty() -> None:
    keys = _env_keys(DEPLOY / "app.env.example")
    for key in SECRET_KEYS & set(keys):
        assert keys[key] == "", key
    assert keys["TELEGRAM_TEST_ENVIRONMENT"] == "false"


def test_staging_template_uses_test_bot_and_op_references() -> None:
    keys = _env_keys(DEPLOY / "staging" / "app.env.tpl")
    assert keys["TELEGRAM_TEST_ENVIRONMENT"] == "true"
    for key in SECRET_KEYS & set(keys):
        assert keys[key].startswith("op://AkkiaBot-Staging/"), key


def test_operations_documents_every_setting() -> None:
    text = OPERATIONS.read_text(encoding="utf-8")
    missing = sorted(name for name in _settings_env_names() if f"`{name}`" not in text)
    assert not missing, f"docs/operations.md misses {missing}"
    compose_vars = _env_keys(DEPLOY / ".env.example")
    assert all(f"`{name}`" in text for name in compose_vars)


# ------------------------------------------------------- docker compose CLI


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False, timeout=30
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _docker_compose_available(), reason="docker compose CLI not installed")
def test_compose_config_resolves(tmp_path: Path) -> None:
    shutil.copy(COMPOSE_FILE, tmp_path / "compose.yaml")
    shutil.copy(DEPLOY / "app.env.example", tmp_path / "app.env")
    env_example = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    (tmp_path / ".env").write_text(
        env_example.replace("POSTGRES_PASSWORD=", "POSTGRES_PASSWORD=fakepasswordfortests").replace(
            "CLOUDFLARED_TOKEN=", "CLOUDFLARED_TOKEN=fake-tunnel-token-for-tests"
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=60,
        env={k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "AKKIABOT_"))},
    )
    assert result.returncode == 0, result.stderr.decode()
    config = json.loads(result.stdout)
    assert config["name"] == "akkiabot"
    app = config["services"]["app"]
    assert app["image"] == "ghcr.io/akkialab/akkiabot:latest"
    assert app["environment"]["DB_URL"].endswith("@postgres:5432/akkiabot")
    assert app["environment"]["TELEGRAM_MODE"] == "polling"  # merged from app.env
    published = [(p.get("host_ip"), int(p["target"])) for p in app["ports"]]
    assert published == [("127.0.0.1", APP_PORT)]


def test_compose_requires_secrets() -> None:
    """Missing POSTGRES_PASSWORD / CLOUDFLARED_TOKEN must fail fast, not default."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "${POSTGRES_PASSWORD:?" in text
    assert "${CLOUDFLARED_TOKEN:?" in text
    assert "${POSTGRES_PASSWORD:-" not in text
    assert "${CLOUDFLARED_TOKEN:-" not in text


# ----------------------------------------------------------------- backup


FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
if [[ "${FAKE_DOCKER_FAIL:-}" == 1 ]]; then exit 3; fi
case "$*" in
  *" pg_dump "*) printf 'PGDMP-fake-dump' ;;
  *" pg_restore "*) cat > "$FAKE_RESTORE_INPUT" ;;
  *" psql "*) echo 0001 ;;
esac
"""


@pytest.fixture
def backup_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    work = tmp_path / "stack"
    work.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_RESTORE_INPUT": str(tmp_path / "restore-input"),
    }
    return work, env


def _run_backup(work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEPLOY / "backup.sh")],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_backup_writes_custom_format_dump(backup_env: tuple[Path, dict[str, str]]) -> None:
    work, env = backup_env
    result = _run_backup(work, env)
    assert result.returncode == 0, result.stderr
    dumps = list((work / "backups").glob("akkiabot-*.dump"))
    assert len(dumps) == 1
    assert dumps[0].read_bytes() == b"PGDMP-fake-dump"
    assert dumps[0].stat().st_mode & 0o077 == 0
    calls = Path(env["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8")
    assert "compose exec -T postgres pg_dump -U akkiabot -d akkiabot --format=custom" in calls


def test_backup_rotates_old_dumps(backup_env: tuple[Path, dict[str, str]]) -> None:
    work, env = backup_env
    backups = work / "backups"
    backups.mkdir()
    for day in range(1, 5):
        (backups / f"akkiabot-2026010{day}T000000Z.dump").write_bytes(b"old")
    (backups / "unrelated.txt").write_text("keep", encoding="utf-8")
    result = _run_backup(work, {**env, "BACKUP_KEEP": "2"})
    assert result.returncode == 0, result.stderr
    remaining = sorted(p.name for p in backups.glob("akkiabot-*.dump"))
    assert len(remaining) == 2
    assert remaining[0] == "akkiabot-20260104T000000Z.dump"  # newest old one survives
    assert (backups / "unrelated.txt").exists()


def test_backup_failure_leaves_no_partial_file(backup_env: tuple[Path, dict[str, str]]) -> None:
    work, env = backup_env
    result = _run_backup(work, {**env, "FAKE_DOCKER_FAIL": "1"})
    assert result.returncode != 0
    assert list((work / "backups").iterdir()) == []


def test_backup_rejects_bad_keep(backup_env: tuple[Path, dict[str, str]]) -> None:
    work, env = backup_env
    result = _run_backup(work, {**env, "BACKUP_KEEP": "0"})
    assert result.returncode == 2


def _cron_command() -> str:
    """The backup command of the crontab line documented in docs/operations.md §6.1."""
    lines = [
        line
        for line in OPERATIONS.read_text(encoding="utf-8").splitlines()
        if "./backup.sh" in line and re.match(r"^(\S+\s+){5}cd ", line)
    ]
    assert len(lines) == 1, lines
    return lines[0].split(maxsplit=5)[5]


def test_documented_cron_line_needs_backups_dir_first(
    backup_env: tuple[Path, dict[str, str]],
) -> None:
    """cron's shell opens backups/backup.log before backup.sh can create the directory."""
    work, env = backup_env
    shutil.copy(DEPLOY / "backup.sh", work / "backup.sh")
    command = _cron_command().replace("/opt/akkiabot/production", str(work))

    # Without the directory the redirection fails and backup.sh never runs.
    failed = subprocess.run(["/bin/sh", "-c", command], env=env, check=False, timeout=60)
    assert failed.returncode != 0
    assert not Path(env["FAKE_DOCKER_LOG"]).exists()

    # The documented init step (deploy §4.2 / §6.1) makes the first scheduled run work.
    init = "install -d -m 700 backups"
    text = OPERATIONS.read_text(encoding="utf-8")
    assert text.count(init) >= 3, "init step missing from deploy / staging / backup sections"
    assert init in (DEPLOY / "backup.sh").read_text(encoding="utf-8")
    subprocess.run(["/bin/sh", "-c", init], cwd=work, env=env, check=True, timeout=60)
    assert (work / "backups").stat().st_mode & 0o777 == 0o700
    ok = subprocess.run(["/bin/sh", "-c", command], env=env, check=False, timeout=60)
    assert ok.returncode == 0
    assert len(list((work / "backups").glob("akkiabot-*.dump"))) == 1
    assert "backup: wrote backups/akkiabot-" in (work / "backups" / "backup.log").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------- restore


def _run_restore(work: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEPLOY / "restore.sh"), *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_restore_recreates_database_without_starting_app(
    backup_env: tuple[Path, dict[str, str]],
) -> None:
    work, env = backup_env
    dump = work / "old.dump"
    dump.write_bytes(b"PGDMP-old-schema")
    result = _run_restore(work, env, "old.dump")
    assert result.returncode == 0, result.stderr
    assert "alembic revision: 0001" in result.stdout
    assert Path(env["FAKE_RESTORE_INPUT"]).read_bytes() == b"PGDMP-old-schema"

    calls = Path(env["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8").splitlines()
    expected = [
        "compose stop app cloudflared",
        "compose up -d --wait postgres",
        "compose exec -T postgres dropdb -U akkiabot --if-exists --force akkiabot",
        "compose exec -T postgres createdb -U akkiabot -O akkiabot akkiabot",
        "compose exec -T postgres pg_restore -U akkiabot -d akkiabot --exit-on-error",
    ]
    assert calls[: len(expected)] == expected
    assert all("psql" in c for c in calls[len(expected) :])
    # Never runs (new-image) migrate or app.
    starts = [c for c in calls if c.startswith(("compose up", "compose run", "compose start"))]
    assert starts == ["compose up -d --wait postgres"]
    assert not [c for c in calls if "migrate" in c]


def test_restore_stops_on_failure(backup_env: tuple[Path, dict[str, str]]) -> None:
    work, env = backup_env
    (work / "old.dump").write_bytes(b"PGDMP")
    result = _run_restore(work, {**env, "FAKE_DOCKER_FAIL": "1"}, "old.dump")
    assert result.returncode != 0
    assert Path(env["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8").count("\n") == 1


@pytest.mark.parametrize("args", [(), ("missing.dump",), ("a", "b")])
def test_restore_rejects_bad_arguments(
    backup_env: tuple[Path, dict[str, str]], args: tuple[str, ...]
) -> None:
    work, env = backup_env
    result = _run_restore(work, env, *args)
    assert result.returncode == 2
    assert not Path(env["FAKE_DOCKER_LOG"]).exists()


def test_documented_migration_rollback_order() -> None:
    """§5.2: pin the old image before restoring; never start anything before the restore."""
    text = OPERATIONS.read_text(encoding="utf-8")
    section = text[text.index("### 5.2 回滚") : text.index("### 5.3")]
    block = section[section.index("```bash") : section.rindex("```")]
    steps = [line for line in block.splitlines() if line and not line.startswith("#")]

    def first(fragment: str) -> int:
        return next(i for i, line in enumerate(steps) if fragment in line)

    stop = first("docker compose stop app cloudflared")
    pin = first("AKKIABOT_TAG=$OLD/")
    compose = first('curl -fsSLo compose.yaml "$base/compose.yaml"')
    restore = first('./restore.sh "$DUMP"')
    start = first("docker compose up -d --wait")
    assert stop < compose < restore
    assert stop < pin < restore < start
    before_restore = steps[:restore]
    assert not [s for s in before_restore if re.search(r"compose (up|run|start)\b", s)]

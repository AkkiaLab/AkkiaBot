"""Guards for deploy/observability: dashboards, alert rules, the Alloy config,
and the script that uploads them (scripts/grafana_sync.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from akkiabot_core.observability import REGISTRY
from scripts import grafana_sync
from scripts.grafana_sync import (
    DataSource,
    SyncError,
    load_dashboards,
    load_rule_group,
    parse_duration,
    pick_datasource,
    rule_group_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
OBSERVABILITY = DEPLOY / "observability"
ALLOY = OBSERVABILITY / "config.alloy"
METRIC = re.compile(r"\bakkiabot_[a-z_]+")


def _exported_series() -> set[str]:
    """Every series name the app can expose, whether or not it has samples yet."""
    names: set[str] = set()
    for family in REGISTRY.collect():
        match family.type:
            case "counter":
                names.add(f"{family.name}_total")
            case "histogram":
                names.update(f"{family.name}_{s}" for s in ("bucket", "count", "sum"))
            case _:
                names.add(family.name)
    return names


def _panels() -> list[dict[str, Any]]:
    (dashboard,) = load_dashboards()
    return [p for p in dashboard["panels"] if p["type"] != "row"]


def _exprs(panels: list[dict[str, Any]], ds_type: str) -> list[str]:
    return [t["expr"] for p in panels if p["datasource"]["type"] == ds_type for t in p["targets"]]


def _rule_exprs() -> list[str]:
    return [
        q["model"]["expr"]
        for rule in load_rule_group()["rules"]
        for q in rule["data"]
        if q["datasourceUid"] != "__expr__"
    ]


# ------------------------------------------------------------- dashboards


def test_dashboard_sections() -> None:
    (dashboard,) = load_dashboards()
    rows = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    assert rows == ["总览", "入群验证", "反垃圾", "群管理", "日志"]
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    assert dashboard["uid"] == "akkiabot-overview"


def test_dashboard_metrics_exist() -> None:
    used = {m for expr in _exprs(_panels(), "prometheus") for m in METRIC.findall(expr)}
    assert used, "no akkiabot_ metrics referenced"
    assert used <= _exported_series(), sorted(used - _exported_series())


def test_dashboard_covers_required_signals() -> None:
    used = " ".join(_exprs(_panels(), "prometheus"))
    for metric in (
        "akkiabot_updates_total",
        "akkiabot_handler_duration_seconds_bucket",
        "akkiabot_handler_errors_total",
        "akkiabot_captcha_started_total",
        "akkiabot_captcha_total",
        "akkiabot_antispam_verdict_total",
        "akkiabot_antispam_llm_duration_seconds_bucket",
        "akkiabot_antispam_llm_errors_total",
        "akkiabot_moderation_actions_total",
        "probe_success",
    ):
        assert metric in used, metric
    for result in ("passed", "timeout", "failed"):
        assert f'result="{result}"' in used


def test_dashboard_queries_are_scoped_to_env_and_use_variables() -> None:
    panels = _panels()
    for panel in panels:
        assert panel["datasource"]["uid"] in {"${prometheus}", "${loki}"}, panel["title"]
        assert panel["targets"], panel["title"]
    for expr in _exprs(panels, "prometheus") + _exprs(panels, "loki"):
        assert 'env="$env"' in expr, expr


def test_logs_filter_by_chat_user_and_trace() -> None:
    (logs,) = [p for p in _panels() if p["type"] == "logs"]
    (expr,) = [t["expr"] for t in logs["targets"]]
    for field in ("chat_id", "user_id", "trace_id"):
        assert f'{field}=~"${field}"' in expr
    (dashboard,) = load_dashboards()
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    for field in ("chat_id", "user_id", "trace_id"):
        assert variables[field]["type"] == "textbox"
        assert variables[field]["query"] == ".*"  # default: everything


# ------------------------------------------------------------ alert rules


def test_alert_rules_cover_required_conditions() -> None:
    rules = {r["uid"]: r for r in load_rule_group()["rules"]}
    assert set(rules) == {
        "akkiabot-polling-stalled",
        "akkiabot-readyz-failing",
        "akkiabot-handler-error-rate",
        "akkiabot-llm-errors",
    }
    for rule in rules.values():
        ref_ids = {q["refId"] for q in rule["data"]}
        assert rule["condition"] in ref_ids
        for query in rule["data"]:
            assert query["datasourceUid"] in {"prometheus", "__expr__"}
            assert query["model"]["refId"] == query["refId"]
        assert rule["annotations"]["summary"]
        assert parse_duration(rule["for"]) >= 60
    # Monitoring down (no probe data) must page too.
    assert rules["akkiabot-readyz-failing"]["noDataState"] == "Alerting"


def test_alert_rule_metrics_exist() -> None:
    used = {m for expr in _rule_exprs() for m in METRIC.findall(expr)}
    assert used <= _exported_series(), sorted(used - _exported_series())
    for expr in _rule_exprs():
        assert "by (env)" in expr, "alerts fire per environment"


def test_alert_file_has_no_env_interpolation() -> None:
    """Grafana expands $VARS in provisioning files."""
    assert "$" not in (OBSERVABILITY / "alerting" / "akkiabot.yaml").read_text(encoding="utf-8")


# ------------------------------------------------------------------ alloy


def _alloy_env_names() -> set[str]:
    return set(re.findall(r'sys\.env\("([A-Z_]+)"\)', ALLOY.read_text(encoding="utf-8")))


@pytest.mark.parametrize("compose", ["compose.yaml", "compose.dev.yaml"])
def test_alloy_env_is_wired_by_compose(compose: str) -> None:
    services = yaml.safe_load((DEPLOY / compose).read_text(encoding="utf-8"))["services"]
    alloy = services["alloy"]
    assert set(alloy["environment"]) == _alloy_env_names()
    assert alloy["image"] == services["alloy"]["image"]
    assert "./observability/config.alloy:/etc/alloy/config.alloy:ro" in alloy["volumes"]


def test_alloy_and_lgtm_images_are_pinned() -> None:
    prod = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))["services"]
    dev = yaml.safe_load((DEPLOY / "compose.dev.yaml").read_text(encoding="utf-8"))["services"]
    assert prod["alloy"]["image"] == dev["alloy"]["image"]
    for image in (prod["alloy"]["image"], dev["lgtm"]["image"]):
        assert re.search(r":v?\d+\.\d+\.\d+$", image), image
    assert dev["alloy"]["profiles"] == dev["lgtm"]["profiles"] == ["observability"]


def test_alloy_keeps_ids_out_of_labels() -> None:
    text = ALLOY.read_text(encoding="utf-8")
    labels = re.search(r"stage\.labels \{(.*?)\n\t\t\}", text, re.S)
    metadata = re.search(r"stage\.structured_metadata \{(.*?)\n\t\t\}", text, re.S)
    assert labels is not None
    assert metadata is not None
    for field in ("chat_id", "user_id", "trace_id"):
        assert field not in labels.group(1), "high-cardinality ids must not become labels"
        assert field in metadata.group(1)


def test_alloy_config_has_no_credentials() -> None:
    text = ALLOY.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "password" in line or "username" in line:
            assert "sys.env(" in line, line


def test_dev_provisioning_mounts_exist() -> None:
    dev = yaml.safe_load((DEPLOY / "compose.dev.yaml").read_text(encoding="utf-8"))["services"]
    for volume in dev["lgtm"]["volumes"]:
        source = volume.split(":")[0]
        if source.startswith("./"):
            assert (DEPLOY / source).exists(), source


# ---------------------------------------------------------- grafana_sync

CLOUD_SOURCES: list[dict[str, Any]] = [
    {"uid": "grafanacloud-prom", "name": "grafanacloud-akkia-prom", "type": "prometheus",
     "url": "https://prometheus-prod.example.net/api/prom", "basicAuthUser": "111", "isDefault": True},
    {"uid": "grafanacloud-usage", "name": "grafanacloud-usage", "type": "prometheus",
     "url": "https://usage.example.net", "basicAuthUser": "9"},
    {"uid": "grafanacloud-logs", "name": "grafanacloud-akkia-logs", "type": "loki",
     "url": "https://logs-prod.example.net", "basicAuthUser": "222"},
    {"uid": "grafanacloud-alert-state-history", "name": "grafanacloud-akkia-alert-state-history",
     "type": "loki", "url": "https://logs-prod.example.net", "basicAuthUser": "222"},
]  # fmt: skip


def test_pick_datasource_prefers_stack_sources() -> None:
    prom = pick_datasource(CLOUD_SOURCES, "prometheus")
    loki = pick_datasource(CLOUD_SOURCES, "loki")
    assert (prom.uid, prom.user) == ("grafanacloud-prom", "111")
    assert prom.push_url == "https://prometheus-prod.example.net/api/prom/push"
    assert loki.push_url == "https://logs-prod.example.net/loki/api/v1/push"
    # otel-lgtm: one source per type, no Cloud naming.
    local = [{"uid": "prometheus", "name": "Prometheus", "type": "prometheus", "url": "x"}]
    assert pick_datasource(local, "prometheus").uid == "prometheus"
    with pytest.raises(SyncError, match="loki"):
        pick_datasource(local, "loki")


def test_rule_group_payload_swaps_datasource() -> None:
    payload = rule_group_payload(load_rule_group(), "grafanacloud-prom")
    assert payload["title"] == "akkiabot"
    assert payload["interval"] == 60
    for rule in payload["rules"]:
        assert rule["folderUID"] == "akkiabot"
        assert rule["ruleGroup"] == "akkiabot"
        uids = {q["datasourceUid"] for q in rule["data"]}
        assert uids == {"grafanacloud-prom", "__expr__"}
    # The loaded file is not mutated.
    assert {q["datasourceUid"] for q in load_rule_group()["rules"][0]["data"]} == {
        "prometheus",
        "__expr__",
    }


def test_parse_duration() -> None:
    assert parse_duration("90s") == 90
    assert parse_duration("2m") == 120
    assert parse_duration("1h") == 3600
    with pytest.raises(SyncError):
        parse_duration("soon")


class FakeGrafana:
    def __init__(self, *, folder_exists: bool = False) -> None:
        self.folder_exists = folder_exists
        self.requests: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == "Bearer fake-grafana-token-for-tests"
        match request.method, request.url.path:
            case "GET", "/api/datasources":
                return httpx.Response(200, json=CLOUD_SOURCES)
            case "GET", "/api/folders/akkiabot":
                return httpx.Response(200 if self.folder_exists else 404, json={})
            case "POST", "/api/dashboards/db":
                return httpx.Response(200, json={"url": "/d/akkiabot-overview/akkiabot"})
            case _:
                return httpx.Response(202, json={})


@pytest.fixture
def grafana_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAFANA_URL", "https://akkia.grafana.example/")
    monkeypatch.setenv("GRAFANA_SA_TOKEN", "fake-grafana-token-for-tests")


@pytest.mark.usefixtures("grafana_env")
def test_sync_uploads_dashboards_and_rules(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGrafana()
    assert grafana_sync.main(["sync"], transport=httpx.MockTransport(fake)) == 0

    calls = [(m, p) for m, p, _ in fake.requests]
    assert calls == [
        ("GET", "/api/datasources"),
        ("GET", "/api/folders/akkiabot"),
        ("POST", "/api/folders"),
        ("POST", "/api/dashboards/db"),
        ("PUT", "/api/v1/provisioning/folder/akkiabot/rule-groups/akkiabot"),
    ]
    _, _, dashboard_body = fake.requests[3]
    assert dashboard_body["overwrite"] is True
    assert dashboard_body["folderUid"] == "akkiabot"
    variables = {v["name"]: v for v in dashboard_body["dashboard"]["templating"]["list"]}
    assert variables["prometheus"]["current"]["value"] == "grafanacloud-prom"
    assert variables["loki"]["current"]["value"] == "grafanacloud-logs"
    _, _, rules = fake.requests[4]
    assert len(rules["rules"]) == 4
    out = capsys.readouterr().out
    assert "4 rules" in out
    assert "fake-grafana-token" not in out


@pytest.mark.usefixtures("grafana_env")
def test_sync_reuses_existing_folder() -> None:
    fake = FakeGrafana(folder_exists=True)
    assert grafana_sync.main(["sync"], transport=httpx.MockTransport(fake)) == 0
    assert ("POST", "/api/folders") not in [(m, p) for m, p, _ in fake.requests]


@pytest.mark.usefixtures("grafana_env")
def test_discover_prints_alloy_endpoints(capsys: pytest.CaptureFixture[str]) -> None:
    assert grafana_sync.main(["discover"], transport=httpx.MockTransport(FakeGrafana())) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["prometheus"]["push_url"].endswith("/api/prom/push")
    assert found["loki"] == {
        "uid": "grafanacloud-logs",
        "name": "grafanacloud-akkia-logs",
        "push_url": "https://logs-prod.example.net/loki/api/v1/push",
        "user": "222",
    }


@pytest.mark.usefixtures("grafana_env")
def test_errors_are_reported_without_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid API key"})

    assert grafana_sync.main(["sync"], transport=httpx.MockTransport(deny)) == 1
    err = capsys.readouterr().err
    assert "HTTP 401" in err
    assert "fake-grafana-token" not in err


def test_missing_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    assert grafana_sync.main(["discover"]) == 1
    assert "GRAFANA_URL" in capsys.readouterr().err


def test_datasource_dataclass_push_url() -> None:
    prom = DataSource("u", "n", "prometheus", "https://p.example/api/prom/", "1")
    assert prom.push_url == "https://p.example/api/prom/push"

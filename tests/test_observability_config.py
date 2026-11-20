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
    env_missing_rules,
    load_dashboards,
    load_env_missing,
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


def _rule_exprs(*, include_dispatch_only: bool = True) -> list[str]:
    return [
        q["model"]["expr"]
        for rule in load_rule_group()["rules"]
        if include_dispatch_only or rule["labels"].get("dispatch_only") != "true"
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
        "akkiabot-dispatch-heartbeat",
    }
    for rule in rules.values():
        ref_ids = {q["refId"] for q in rule["data"]}
        assert rule["condition"] in ref_ids
        for query in rule["data"]:
            assert query["datasourceUid"] in {"prometheus", "loki", "__expr__"}
            assert query["model"]["refId"] == query["refId"]
        assert rule["annotations"]["summary"]
        # Every rule reaches the dispatch relay, which only accepts these envs.
        assert rule["labels"].get("env", "production") in {"production", "staging"}
        if rule["labels"].get("dispatch_only") != "true":
            assert parse_duration(rule["for"]) >= 60
    # Monitoring down (no probe data) must page too.
    assert rules["akkiabot-readyz-failing"]["noDataState"] == "Alerting"


def test_alert_rule_metrics_exist() -> None:
    used = {m for expr in _rule_exprs() for m in METRIC.findall(expr)}
    assert used <= _exported_series(), sorted(used - _exported_series())
    for expr in _rule_exprs(include_dispatch_only=False):
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
        assert rule["labels"]["akkiabot_rule"] == rule["uid"]
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
    def __init__(self, *, folder_exists: bool = False, list_hides_user: bool = False) -> None:
        self.folder_exists = folder_exists
        self.list_hides_user = list_hides_user
        self.requests: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == "Bearer fake-grafana-token-for-tests"
        match request.method, request.url.path:
            case "GET", "/api/datasources":
                if self.list_hides_user:
                    hidden = [
                        {k: v for k, v in s.items() if k != "basicAuthUser"} for s in CLOUD_SOURCES
                    ]
                    return httpx.Response(200, json=hidden)
                return httpx.Response(200, json=CLOUD_SOURCES)
            case "GET", path if path.startswith("/api/datasources/uid/"):
                uid = path.rsplit("/", 1)[-1]
                return httpx.Response(200, json=next(s for s in CLOUD_SOURCES if s["uid"] == uid))
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
    uids = [r["uid"] for r in rules["rules"]]
    assert len(uids) == 7
    assert all(r["labels"]["akkiabot_rule"] == r["uid"] for r in rules["rules"])
    assert uids[-2:] == [  # default expected envs
        "akkiabot-env-missing-staging",
        "akkiabot-env-missing-production",
    ]
    out = capsys.readouterr().out
    assert "7 rules" in out
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
def test_discover_reads_instance_id_from_datasource_detail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Regression: Grafana Cloud's /api/datasources list has no basicAuthUser
    # (seen 2026-11-15), so discover printed an empty Alloy user.
    fake = FakeGrafana(list_hides_user=True)
    assert grafana_sync.main(["discover"], transport=httpx.MockTransport(fake)) == 0
    found = json.loads(capsys.readouterr().out)
    assert (found["prometheus"]["user"], found["loki"]["user"]) == ("111", "222")
    paths = [p for _, p, _ in fake.requests]
    assert "/api/datasources/uid/grafanacloud-prom" in paths


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


def test_readyz_probe_has_the_job_the_queries_use() -> None:
    """The blackbox exporter would label it job="integrations/blackbox/readyz"."""
    text = ALLOY.read_text(encoding="utf-8")
    assert 'replacement  = "akkiabot-readyz"' in text
    assert "targets         = discovery.relabel.readyz.output" in text
    exprs = _exprs(_panels(), "prometheus") + _rule_exprs()
    probes = [e for e in exprs if "probe_success" in e]
    assert probes
    assert all('job="akkiabot-readyz"' in e for e in probes)


# ------------------------------------------------- per-environment absence


def test_env_missing_rule_per_expected_environment() -> None:
    template = load_env_missing()
    assert template["environments"] == ["staging", "production"]
    rules = env_missing_rules(template, ["staging", "production"])
    assert [r["uid"] for r in rules] == [
        "akkiabot-env-missing-staging",
        "akkiabot-env-missing-production",
    ]
    for rule, env in zip(rules, ["staging", "production"], strict=True):
        (expr,) = [q["model"]["expr"] for q in rule["data"] if q["datasourceUid"] == "prometheus"]
        assert expr == f'absent(probe_success{{job="akkiabot-readyz", env="{env}"}})'
        assert "__ENV__" not in json.dumps(rule, ensure_ascii=False)
        assert env in rule["title"]
        assert rule["noDataState"] == "OK"  # present = absent() returns nothing
    with pytest.raises(SyncError):
        env_missing_rules(template, ['staging"} or vector(1) #'])


def test_env_missing_template_is_kept_out_of_provisioning() -> None:
    """lgtm only loads akkiabot.yaml; the template must not leak an __ENV__ rule there."""
    dev = yaml.safe_load((DEPLOY / "compose.dev.yaml").read_text(encoding="utf-8"))["services"]
    assert not any("env-missing" in v for v in dev["lgtm"]["volumes"])
    assert "$" not in (OBSERVABILITY / "alerting" / "env-missing.yaml").read_text(encoding="utf-8")


@pytest.mark.usefixtures("grafana_env")
def test_sync_env_option_overrides_expected_envs() -> None:
    fake = FakeGrafana()
    argv = ["sync", "--env", "staging"]
    assert grafana_sync.main(argv, transport=httpx.MockTransport(fake)) == 0
    _, _, rules = fake.requests[-1]
    uids = [r["uid"] for r in rules["rules"]]
    assert uids[-1] == "akkiabot-env-missing-staging"
    assert "akkiabot-env-missing-production" not in uids
    assert all(r["ruleGroup"] == "akkiabot" for r in rules["rules"])


# ------------------------------------------------- alert dispatch (AKKIA-28)


def test_rule_group_payload_maps_loki_and_refuses_unmapped_loki() -> None:
    group = {
        "name": "g",
        "interval": "1m",
        "rules": [
            {
                "uid": "akkiabot-fictional",
                "title": "t",
                "condition": "A",
                "data": [{"refId": "A", "datasourceUid": "loki", "model": {"refId": "A"}}],
                "noDataState": "OK",
                "execErrState": "Error",
                "for": "0s",
            }
        ],
    }
    payload = rule_group_payload(group, "grafanacloud-prom", "grafanacloud-logs")
    assert payload["rules"][0]["data"][0]["datasourceUid"] == "grafanacloud-logs"
    with pytest.raises(SyncError, match="Loki"):
        rule_group_payload(group, "grafanacloud-prom")


def test_heartbeat_rule_needs_no_data_and_skips_email() -> None:
    rules = {r["uid"]: r for r in load_rule_group()["rules"]}
    heartbeat = rules["akkiabot-dispatch-heartbeat"]
    (query,) = [q for q in heartbeat["data"] if q["datasourceUid"] == "prometheus"]
    assert not METRIC.search(query["model"]["expr"])
    assert heartbeat["labels"]["dispatch_only"] == "true"
    assert heartbeat["noDataState"] == "OK"
    # Every other rule keeps its email notification.
    assert [u for u, r in rules.items() if r["labels"].get("dispatch_only")] == [
        "akkiabot-dispatch-heartbeat"
    ]


EXISTING_TREE = {
    "receiver": "grafana-default-email",
    "group_by": ["grafana_folder", "alertname"],
    "routes": [
        {"receiver": "someone-else", "object_matchers": [["team", "=", "fictional"]]},
        {"receiver": "stale", "object_matchers": [["akkiabot_rule", "=~", "old"]]},
    ],
}


def test_policy_merge_keeps_root_and_foreign_routes() -> None:
    config = grafana_sync.load_notifications()
    merged = grafana_sync.merge_policy_tree(EXISTING_TREE, config["routes"])
    assert merged["receiver"] == "grafana-default-email"
    assert merged["group_by"] == ["grafana_folder", "alertname"]
    dispatch, email, foreign = merged["routes"]
    assert dispatch["receiver"] == "akkiabot-dispatch"
    assert dispatch["continue"] is True
    assert dispatch["group_by"] == ["..."]
    # The rules that emailed through the root still email, dispatch-only ones do not.
    assert email["receiver"] == "grafana-default-email"
    assert ["dispatch_only", "!=", "true"] in email["object_matchers"]
    assert ["drill", "!=", "true"] in email["object_matchers"]
    assert "continue" not in email
    assert foreign == EXISTING_TREE["routes"][0]
    # Idempotent: a second merge replaces our routes instead of stacking them.
    assert grafana_sync.merge_policy_tree(merged, config["routes"]) == merged
    assert "$" not in (OBSERVABILITY / "alerting" / "notifications.yaml").read_text(
        encoding="utf-8"
    )


class NotifyGrafana(FakeGrafana):
    def __init__(self, *, contact_exists: bool) -> None:
        super().__init__()
        self.contact_exists = contact_exists
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        match request.method, request.url.path:
            case "GET", "/api/v1/provisioning/contact-points":
                self.requests.append(("GET", request.url.path, None))
                points = [{"uid": "", "name": "email receiver", "type": "email"}]
                if self.contact_exists:
                    points.append({"uid": "akkiabot-dispatch", "name": "akkiabot-dispatch"})
                return httpx.Response(200, json=points)
            case "GET", "/api/v1/provisioning/policies":
                self.requests.append(("GET", request.url.path, None))
                return httpx.Response(200, json=EXISTING_TREE)
        return super().__call__(request)


@pytest.mark.usefixtures("grafana_env")
@pytest.mark.parametrize("exists", [False, True])
def test_notifications_writes_contact_point_and_policy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exists: bool
) -> None:
    monkeypatch.setenv("GRAFANA_DISPATCH_WEBHOOK_URL", "https://relay.example.invalid/")
    # mock: fictional HMAC secret, not a real credential
    mock_secret = "mock-hmac-secret-for-tests"
    monkeypatch.setenv("GRAFANA_DISPATCH_HMAC_SECRET", mock_secret)
    fake = NotifyGrafana(contact_exists=exists)
    assert grafana_sync.main(["notifications"], transport=httpx.MockTransport(fake)) == 0
    calls = [(m, p) for m, p, _ in fake.requests]
    write = (
        ("PUT", "/api/v1/provisioning/contact-points/akkiabot-dispatch")
        if exists
        else ("POST", "/api/v1/provisioning/contact-points")
    )
    assert calls == [
        ("GET", "/api/v1/provisioning/contact-points"),
        write,
        ("GET", "/api/v1/provisioning/policies"),
        ("PUT", "/api/v1/provisioning/policies"),
    ]
    contact = fake.requests[1][2]
    assert contact["type"] == "webhook"
    assert contact["settings"]["url"] == "https://relay.example.invalid/"
    assert contact["settings"]["hmacConfig"] == {
        "header": "X-Grafana-Alerting-Signature",
        "timestampHeader": "X-Grafana-Alerting-Timestamp",
        "secret": mock_secret,
    }
    assert fake.headers[-1]["X-Disable-Provenance"] == "true"
    assert fake.requests[3][2]["routes"][0]["receiver"] == "akkiabot-dispatch"
    out = capsys.readouterr().out
    assert mock_secret not in out
    assert "relay.example" not in out


@pytest.mark.usefixtures("grafana_env")
def test_notifications_requires_relay_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GRAFANA_DISPATCH_WEBHOOK_URL", "http://insecure.example.invalid/")
    fake = NotifyGrafana(contact_exists=False)
    assert grafana_sync.main(["notifications"], transport=httpx.MockTransport(fake)) == 1
    assert "https" in capsys.readouterr().err
    assert fake.requests == []


@pytest.mark.usefixtures("grafana_env")
def test_drill_start_and_stop(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGrafana(folder_exists=True)
    argv = ["drill", "start", "--rule", "akkiabot-drill-alert"]
    assert grafana_sync.main(argv, transport=httpx.MockTransport(fake)) == 0
    (put,) = [b for m, p, b in fake.requests if m == "PUT"]
    assert put["title"] == "akkiabot-drill"
    (rule,) = put["rules"]
    assert rule["labels"] == {
        "severity": "warning",
        "service": "akkiabot",
        "env": "staging",
        "drill": "true",
        "akkiabot_rule": "akkiabot-drill-alert",
    }
    assert {q["datasourceUid"] for q in rule["data"]} == {"grafanacloud-prom", "__expr__"}
    assert grafana_sync.main(["drill", "stop"], transport=httpx.MockTransport(fake)) == 0
    assert fake.requests[-1][:2] == (
        "DELETE",
        "/api/v1/provisioning/folder/akkiabot/rule-groups/akkiabot-drill",
    )
    assert grafana_sync.main(["drill", "start", "--rule", "nope"]) == 1
    drill = yaml.safe_load((OBSERVABILITY / "alerting" / "drill.yaml").read_text(encoding="utf-8"))
    for item in drill["rules"]:
        assert item["labels"]["env"] == "staging"

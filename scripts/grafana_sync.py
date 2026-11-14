"""Upload the versioned dashboards and alert rules to a Grafana instance.

The repository is the source of truth (``deploy/observability/``); this script
makes a Grafana Cloud stack match it:

* ``discover`` — print the stack's Prometheus / Loki data sources and the
  push endpoints Alloy needs (URLs and instance ids, no secrets);
* ``sync`` — create the ``AkkiaBot`` folder, upload every dashboard
  (overwriting) and replace the ``akkiabot`` alert rule group.

Credentials come from the environment, never from arguments::

    GRAFANA_URL       https://<stack>.grafana.net
    GRAFANA_SA_TOKEN  service account token (role Admin, or Editor + alerting
                      provisioning rights)

Typical use (1Password item ``staging-grafana``)::

    op run --env-file deploy/observability/grafana-sync.env.tpl -- just grafana-sync sync

Local grafana/otel-lgtm needs no token: ``GRAFANA_URL=http://127.0.0.1:3000``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY = ROOT / "deploy" / "observability"
DASHBOARDS = OBSERVABILITY / "dashboards"
ALERT_RULES = OBSERVABILITY / "alerting" / "akkiabot.yaml"

FOLDER_UID = "akkiabot"
FOLDER_TITLE = "AkkiaBot"
#: Data source uid the rule file uses (the one grafana/otel-lgtm provisions).
LOCAL_PROMETHEUS_UID = "prometheus"

_DURATION = re.compile(r"^(\d+)([smh])$")
_SECONDS = {"s": 1, "m": 60, "h": 3600}


class SyncError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class DataSource:
    uid: str
    name: str
    type: str
    url: str
    user: str

    @property
    def push_url(self) -> str:
        """Where Alloy writes to (Grafana Cloud layout)."""
        base = self.url.rstrip("/")
        return f"{base}/push" if self.type == "prometheus" else f"{base}/loki/api/v1/push"


def parse_duration(value: str | int) -> int:
    if isinstance(value, int):
        return value
    match = _DURATION.match(value)
    if match is None:
        raise SyncError(f"无法解析时长：{value!r}")
    return int(match.group(1)) * _SECONDS[match.group(2)]


def pick_datasource(sources: Sequence[dict[str, Any]], kind: str) -> DataSource:
    """The stack's own Prometheus / Loki data source.

    Grafana Cloud provisions ``grafanacloud-<stack>-prom`` / ``-logs`` next to
    usage-insights and alert-state-history sources of the same type; prefer
    those names, then the default source, then the only one.
    """
    candidates = [s for s in sources if s.get("type") == kind]
    suffix = "-prom" if kind == "prometheus" else "-logs"
    for pool in (
        [s for s in candidates if str(s.get("name", "")).endswith(suffix)],
        [s for s in candidates if s.get("isDefault")],
        candidates,
    ):
        if len(pool) == 1:
            s = pool[0]
            return DataSource(
                uid=str(s["uid"]),
                name=str(s.get("name", "")),
                type=kind,
                url=str(s.get("url", "")),
                user=str(s.get("basicAuthUser", "")),
            )
    names = sorted(str(s.get("name")) for s in candidates)
    raise SyncError(f"找不到唯一的 {kind} 数据源（候选：{names}），用 --{kind}-uid 指定")


def load_dashboards(directory: Path = DASHBOARDS) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))
    ]


def load_rule_group(path: Path = ALERT_RULES) -> dict[str, Any]:
    (group,) = yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]
    return group


def pin_datasources(dashboard: dict[str, Any], uids: dict[str, DataSource]) -> dict[str, Any]:
    """Preselect the stack's data sources in the ``prometheus`` / ``loki`` variables."""
    pinned = copy.deepcopy(dashboard)
    pinned.pop("id", None)
    for variable in pinned.get("templating", {}).get("list", []):
        source = (
            uids.get(variable.get("query", "")) if variable.get("type") == "datasource" else None
        )
        if source is not None:
            variable["current"] = {"text": source.name, "value": source.uid}
    return pinned


def rule_group_payload(group: dict[str, Any], prometheus_uid: str) -> dict[str, Any]:
    """Convert a file-provisioning rule group into the provisioning API body."""
    rules = []
    for rule in group["rules"]:
        data = []
        for query in rule["data"]:
            query = copy.deepcopy(query)
            if query["datasourceUid"] == LOCAL_PROMETHEUS_UID:
                query["datasourceUid"] = prometheus_uid
            data.append(query)
        rules.append(
            {
                "uid": rule["uid"],
                "title": rule["title"],
                "condition": rule["condition"],
                "data": data,
                "noDataState": rule["noDataState"],
                "execErrState": rule["execErrState"],
                "for": rule["for"],
                "labels": rule.get("labels", {}),
                "annotations": rule.get("annotations", {}),
                "folderUID": FOLDER_UID,
                "ruleGroup": group["name"],
                "orgID": group.get("orgId", 1),
            }
        )
    return {
        "title": group["name"],
        "folderUid": FOLDER_UID,
        "interval": parse_duration(group["interval"]),
        "rules": rules,
    }


class Grafana:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def _check(self, response: httpx.Response, action: str) -> httpx.Response:
        if response.is_error:
            # The body is Grafana's error message; it never echoes the token.
            raise SyncError(f"{action} 失败：HTTP {response.status_code} {response.text[:300]}")
        return response

    def datasources(self) -> list[dict[str, Any]]:
        return self._check(self.client.get("/api/datasources"), "读取数据源").json()

    def ensure_folder(self) -> None:
        response = self.client.get(f"/api/folders/{FOLDER_UID}")
        if response.status_code == 404:
            self._check(
                self.client.post("/api/folders", json={"uid": FOLDER_UID, "title": FOLDER_TITLE}),
                "创建文件夹",
            )
        else:
            self._check(response, "读取文件夹")

    def put_dashboard(self, dashboard: dict[str, Any]) -> str:
        body = {
            "dashboard": dashboard,
            "folderUid": FOLDER_UID,
            "overwrite": True,
            "message": "scripts/grafana_sync.py",
        }
        response = self._check(
            self.client.post("/api/dashboards/db", json=body), f"上传 dashboard {dashboard['uid']}"
        )
        return str(response.json().get("url", ""))

    def put_rule_group(self, payload: dict[str, Any]) -> None:
        self._check(
            self.client.put(
                f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{payload['title']}",
                json=payload,
            ),
            f"上传告警规则组 {payload['title']}",
        )


def discover(grafana: Grafana) -> dict[str, dict[str, str]]:
    sources = grafana.datasources()
    out: dict[str, dict[str, str]] = {}
    for kind in ("prometheus", "loki"):
        ds = pick_datasource(sources, kind)
        out[kind] = {"uid": ds.uid, "name": ds.name, "push_url": ds.push_url, "user": ds.user}
    return out


def sync(
    grafana: Grafana, *, prometheus_uid: str | None = None, loki_uid: str | None = None
) -> list[str]:
    sources = grafana.datasources()
    picked = {
        "prometheus": _override(sources, "prometheus", prometheus_uid),
        "loki": _override(sources, "loki", loki_uid),
    }
    grafana.ensure_folder()
    report = []
    for dashboard in load_dashboards():
        url = grafana.put_dashboard(pin_datasources(dashboard, picked))
        report.append(f"dashboard {dashboard['uid']}: {url}")
    payload = rule_group_payload(load_rule_group(), picked["prometheus"].uid)
    grafana.put_rule_group(payload)
    report.append(f"alert rule group {payload['title']}: {len(payload['rules'])} rules")
    return report


def _override(sources: Sequence[dict[str, Any]], kind: str, uid: str | None) -> DataSource:
    if uid is None:
        return pick_datasource(sources, kind)
    return pick_datasource([s for s in sources if s.get("uid") == uid], kind)


def client_from_env(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    url = os.environ.get("GRAFANA_URL", "").strip()
    if not url:
        raise SyncError("缺少环境变量 GRAFANA_URL")
    token = os.environ.get("GRAFANA_SA_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=30, transport=transport)


def main(argv: Sequence[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grafana_sync", description="Upload AkkiaBot dashboards and alert rules to Grafana."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="打印 Prometheus / Loki 数据源与 Alloy 推送地址")
    sync_cmd = sub.add_parser("sync", help="上传 dashboard 与告警规则")
    sync_cmd.add_argument("--prometheus-uid")
    sync_cmd.add_argument("--loki-uid")
    args = parser.parse_args(argv)
    try:
        with client_from_env(transport) as client:
            grafana = Grafana(client)
            if args.command == "discover":
                print(json.dumps(discover(grafana), ensure_ascii=False, indent=2))
            else:
                for line in sync(
                    grafana, prometheus_uid=args.prometheus_uid, loki_uid=args.loki_uid
                ):
                    print(line)
    except (SyncError, httpx.HTTPError) as exc:
        print(f"grafana_sync: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

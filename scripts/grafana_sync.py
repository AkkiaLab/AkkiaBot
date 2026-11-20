"""Upload the versioned dashboards and alert rules to a Grafana instance.

The repository is the source of truth (``deploy/observability/``); this script
makes a Grafana Cloud stack match it:

* ``discover`` — print the stack's Prometheus / Loki data sources and the
  push endpoints Alloy needs (URLs and instance ids, no secrets);
* ``sync`` — create the ``AkkiaBot`` folder, upload every dashboard
  (overwriting) and replace the ``akkiabot`` alert rule group;
* ``notifications`` — create or update the webhook contact point of the alert
  dispatch relay and merge its routes into the notification policy tree
  (``alerting/notifications.yaml``, docs/grafana-alerting.md);
* ``drill start|stop`` — upload or remove the drill rule group
  (``alerting/drill.yaml``) used to exercise the dispatch chain on staging.

Credentials come from the environment, never from arguments::

    GRAFANA_URL       https://<stack>.grafana.net
    GRAFANA_SA_TOKEN  service account token (role Admin, or Editor + alerting
                      provisioning rights)

Typical use (1Password item ``staging-grafana``)::

    op run --env-file deploy/observability/grafana-sync.env.tpl -- just grafana-sync sync

Local grafana/otel-lgtm needs no token: ``GRAFANA_URL=http://127.0.0.1:3000``.

``notifications`` also needs the relay endpoint and its HMAC secret
(``GRAFANA_DISPATCH_WEBHOOK_URL``, ``GRAFANA_DISPATCH_HMAC_SECRET``, 1Password
item ``grafana-webhook``, see ``grafana-notify.env.tpl``).
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
ENV_MISSING = OBSERVABILITY / "alerting" / "env-missing.yaml"
NOTIFICATIONS = OBSERVABILITY / "alerting" / "notifications.yaml"
DRILL = OBSERVABILITY / "alerting" / "drill.yaml"
ROOT_RECEIVER_PLACEHOLDER = "__ROOT_RECEIVER__"
#: Every rule carries its uid as a label: notifications drop internal labels
#: (``__alert_rule_uid__``), and the dispatch routes and relay match on it.
RULE_LABEL = "akkiabot_rule"
ENV_PLACEHOLDER = "__ENV__"
_ENV_NAME = re.compile(r"^[a-z][a-z0-9-]*$")

FOLDER_UID = "akkiabot"
FOLDER_TITLE = "AkkiaBot"
#: Data source uids the rule files use (the ones grafana/otel-lgtm provisions).
LOCAL_PROMETHEUS_UID = "prometheus"
LOCAL_LOKI_UID = "loki"

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


def load_env_missing(path: Path = ENV_MISSING) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_missing_rules(template: dict[str, Any], envs: Sequence[str]) -> list[dict[str, Any]]:
    """One "environment stopped reporting" rule per expected environment."""
    rules = []
    for env in envs:
        if not _ENV_NAME.match(env):
            raise SyncError(f"环境名不合法：{env!r}")
        text = json.dumps(template["rule"], ensure_ascii=False).replace(ENV_PLACEHOLDER, env)
        rules.append(json.loads(text))
    return rules


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


def rule_group_payload(
    group: dict[str, Any], prometheus_uid: str, loki_uid: str | None = None
) -> dict[str, Any]:
    """Convert a file-provisioning rule group into the provisioning API body."""
    swap = {LOCAL_PROMETHEUS_UID: prometheus_uid}
    if loki_uid:
        swap[LOCAL_LOKI_UID] = loki_uid
    rules = []
    for rule in group["rules"]:
        data = []
        for query in rule["data"]:
            query = copy.deepcopy(query)
            if query["datasourceUid"] in swap:
                query["datasourceUid"] = swap[query["datasourceUid"]]
            elif query["datasourceUid"] == LOCAL_LOKI_UID:
                raise SyncError(f"规则 {rule['uid']} 使用 Loki，需要 Loki 数据源 uid")
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
                "labels": {**rule.get("labels", {}), RULE_LABEL: rule["uid"]},
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

    def datasource(self, uid: str) -> dict[str, Any]:
        return self._check(
            self.client.get(f"/api/datasources/uid/{uid}"), f"读取数据源 {uid}"
        ).json()

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

    def delete_rule_group(self, name: str) -> bool:
        response = self.client.delete(
            f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{name}"
        )
        if response.status_code == 404:
            return False
        self._check(response, f"删除告警规则组 {name}")
        return True

    def contact_points(self) -> list[dict[str, Any]]:
        return self._check(
            self.client.get("/api/v1/provisioning/contact-points"), "读取 contact point"
        ).json()

    def put_contact_point(self, contact_point: dict[str, Any], *, exists: bool) -> None:
        if exists:
            response = self.client.put(
                f"/api/v1/provisioning/contact-points/{contact_point['uid']}", json=contact_point
            )
        else:
            response = self.client.post("/api/v1/provisioning/contact-points", json=contact_point)
        # The request carries the relay URL and HMAC secret, and an error body
        # may echo request fields: report the operation and status only.
        if response.is_error:
            raise SyncError(
                f"写入 contact point {contact_point['uid']} 失败：HTTP {response.status_code}"
            )

    def policies(self) -> dict[str, Any]:
        return self._check(
            self.client.get("/api/v1/provisioning/policies"), "读取 notification policy"
        ).json()

    def put_policies(self, tree: dict[str, Any]) -> None:
        # Without provenance the tree stays editable in the Grafana UI (the
        # owner's email routing); the next run re-applies only our routes.
        self._check(
            self.client.put(
                "/api/v1/provisioning/policies",
                json=tree,
                headers={"X-Disable-Provenance": "true"},
            ),
            "写入 notification policy",
        )


def load_notifications(path: Path = NOTIFICATIONS) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _owned_route(route: dict[str, Any]) -> bool:
    """Routes this script manages: they match on the rule-uid label."""
    return any(m and m[0] == RULE_LABEL for m in route.get("object_matchers") or [])


def merge_policy_tree(tree: dict[str, Any], routes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Put our routes first under the root, keep every other route and the root."""
    merged = copy.deepcopy(tree)
    root_receiver = merged.get("receiver")
    if not root_receiver:
        raise SyncError("notification policy 没有根 receiver")
    ours = json.loads(
        json.dumps(list(routes)).replace(
            f'"{ROOT_RECEIVER_PLACEHOLDER}"', json.dumps(root_receiver)
        )
    )
    others = [r for r in merged.get("routes") or [] if not _owned_route(r)]
    merged["routes"] = [*ours, *others]
    return merged


def notifications(grafana: Grafana, *, url: str, secret: str) -> list[str]:
    if not url.startswith("https://"):
        raise SyncError("GRAFANA_DISPATCH_WEBHOOK_URL 必须是 https 地址")
    if not secret:
        raise SyncError("缺少环境变量 GRAFANA_DISPATCH_HMAC_SECRET")
    config = load_notifications()
    contact_point = copy.deepcopy(config["contact_point"])
    contact_point["settings"]["url"] = url
    contact_point["settings"]["hmacConfig"]["secret"] = secret
    existing = {c.get("uid") for c in grafana.contact_points()}
    grafana.put_contact_point(contact_point, exists=contact_point["uid"] in existing)
    tree = merge_policy_tree(grafana.policies(), config["routes"])
    grafana.put_policies(tree)
    kept = len(tree["routes"]) - len(config["routes"])
    return [
        f"contact point {contact_point['uid']}: {'updated' if contact_point['uid'] in existing else 'created'}",
        f"notification policy: {len(config['routes'])} dispatch routes first, "
        f"{kept} other routes and root receiver {tree['receiver']} kept",
    ]


def load_drill(path: Path = DRILL) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def drill_group(drill: dict[str, Any], only: Sequence[str] | None = None) -> dict[str, Any]:
    known = [r["uid"] for r in drill["rules"]]
    unknown = sorted(set(only or []) - set(known))
    if unknown:
        raise SyncError(f"未知的演练规则：{unknown}（可选：{known}）")
    rules = []
    for rule in drill["rules"]:
        if only and rule["uid"] not in only:
            continue
        rules.append({**copy.deepcopy(drill["template"]), **rule})
    return {"name": drill["name"], "interval": drill["interval"], "rules": rules}


def discover(grafana: Grafana) -> dict[str, dict[str, str]]:
    sources = grafana.datasources()
    out: dict[str, dict[str, str]] = {}
    for kind in ("prometheus", "loki"):
        ds = pick_datasource(sources, kind)
        # Grafana Cloud's list endpoint omits basicAuthUser (the instance id Alloy
        # authenticates as); only the per-uid endpoint returns it.
        user = ds.user or str(grafana.datasource(ds.uid).get("basicAuthUser", ""))
        out[kind] = {"uid": ds.uid, "name": ds.name, "push_url": ds.push_url, "user": user}
    return out


def sync(
    grafana: Grafana,
    *,
    prometheus_uid: str | None = None,
    loki_uid: str | None = None,
    envs: Sequence[str] | None = None,
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
    template = load_env_missing()
    expected = list(envs) if envs else list(template["environments"])
    group = load_rule_group()
    group["rules"] = [*group["rules"], *env_missing_rules(template, expected)]
    payload = rule_group_payload(group, picked["prometheus"].uid, picked["loki"].uid)
    grafana.put_rule_group(payload)
    report.append(
        f"alert rule group {payload['title']}: {len(payload['rules'])} rules "
        f"(expected envs: {', '.join(expected)})"
    )
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
    sync_cmd.add_argument(
        "--env",
        action="append",
        dest="envs",
        help="必须持续上报的环境（可重复），默认取 alerting/env-missing.yaml 的 environments",
    )
    sub.add_parser("notifications", help="写入告警分派 webhook contact point，并合并通知策略路由")
    drill_cmd = sub.add_parser("drill", help="上传或删除告警分派演练规则组（只用于 staging 演练）")
    drill_cmd.add_argument("state", choices=["start", "stop"])
    drill_cmd.add_argument(
        "--rule", action="append", dest="rules", help="只上传指定 uid 的演练规则（可重复）"
    )
    args = parser.parse_args(argv)
    try:
        with client_from_env(transport) as client:
            grafana = Grafana(client)
            if args.command == "discover":
                print(json.dumps(discover(grafana), ensure_ascii=False, indent=2))
            elif args.command == "notifications":
                for line in notifications(
                    grafana,
                    url=os.environ.get("GRAFANA_DISPATCH_WEBHOOK_URL", "").strip(),
                    secret=os.environ.get("GRAFANA_DISPATCH_HMAC_SECRET", "").strip(),
                ):
                    print(line)
            elif args.command == "drill":
                group = drill_group(load_drill(), args.rules)
                if args.state == "stop":
                    removed = grafana.delete_rule_group(group["name"])
                    print(f"drill group {group['name']}: {'removed' if removed else 'not present'}")
                else:
                    grafana.ensure_folder()
                    prometheus = _override(grafana.datasources(), "prometheus", None)
                    payload = rule_group_payload(group, prometheus.uid)
                    grafana.put_rule_group(payload)
                    uids = ", ".join(r["uid"] for r in payload["rules"])
                    print(f"drill group {group['name']}: {uids}")
            else:
                for line in sync(
                    grafana,
                    prometheus_uid=args.prometheus_uid,
                    loki_uid=args.loki_uid,
                    envs=args.envs,
                ):
                    print(line)
    except (SyncError, httpx.HTTPError) as exc:
        print(f"grafana_sync: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

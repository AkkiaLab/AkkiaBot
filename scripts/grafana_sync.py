"""Upload the versioned dashboards and alert rules to a Grafana instance.

The repository is the source of truth (``deploy/observability/``); this script
makes a Grafana Cloud stack match it:

* ``discover`` — print the stack's Prometheus / Loki data sources and the
  push endpoints Alloy needs (URLs and instance ids, no secrets);
* ``sync`` — create the ``AkkiaBot`` folder, upload every dashboard
  (overwriting) and replace the ``akkiabot`` alert rule group;
* ``notifications`` — optional: create or update a signed webhook contact
  point and merge its routes into the notification policy tree
  (``alerting/notifications.yaml``, docs/observability.md §5.1). Skipped when
  no webhook URL is configured.

Credentials come from the environment, never from arguments::

    GRAFANA_URL       https://<stack>.grafana.net
    GRAFANA_SA_TOKEN  service account token (role Admin, or Editor + alerting
                      provisioning rights)

Typical use (1Password item ``staging-grafana``)::

    op run --env-file deploy/observability/grafana-sync.env.tpl -- just grafana-sync sync

Local grafana/otel-lgtm needs no token: ``GRAFANA_URL=http://127.0.0.1:3000``.

``notifications`` reads the webhook endpoint and its HMAC secret from
``GRAFANA_DISPATCH_WEBHOOK_URL`` / ``GRAFANA_DISPATCH_HMAC_SECRET`` (see
``grafana-notify.env.tpl``); without a URL it changes nothing.
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
#: Every rule carries its uid as a label: notifications drop internal labels
#: (``__alert_rule_uid__``); webhook routes and receivers can match on it.
RULE_LABEL = "akkiabot_rule"
ENV_PLACEHOLDER = "__ENV__"
# Minutes from the last sample to firing: Prometheus lookback (5m) plus `for`.
MINUTES_PLACEHOLDER = "__MINUTES__"
_LOOKBACK_SECONDS = 300
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
    overrides = template.get("for_by_env") or {}
    rules = []
    for env in envs:
        if not _ENV_NAME.match(env):
            raise SyncError(f"环境名不合法：{env!r}")
        rule = copy.deepcopy(template["rule"])
        rule["for"] = overrides.get(env, rule["for"])
        minutes = (_LOOKBACK_SECONDS + parse_duration(rule["for"])) // 60
        text = (
            json.dumps(rule, ensure_ascii=False)
            .replace(ENV_PLACEHOLDER, env)
            .replace(MINUTES_PLACEHOLDER, str(minutes))
        )
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
        # The request carries the webhook URL and HMAC secret, and an error body
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


NEGATED_OP = {"=": "!=", "!=": "=", "=~": "!~", "!~": "=~"}


def _owned_route(route: dict[str, Any], marker: list[str]) -> bool:
    """Routes this script manages carry the marker matcher."""
    return marker in (route.get("object_matchers") or [])


def _not_matched_by(route: dict[str, Any]) -> list[list[list[str]]]:
    """Disjoint matcher sets whose union is "this route does not match".

    NOT(m1 AND m2 AND m3) = [!m1] | [m1, !m2] | [m1, m2, !m3]; the parts never
    overlap, so at most one of the routes built from them matches an alert.
    """
    if route.get("matchers") or route.get("match") or route.get("match_re"):
        raise SyncError(
            "通知策略中有使用旧式 matchers 的 continue 子路由，无法自动合并，请改为 object_matchers"
        )
    matchers = [list(m) for m in route.get("object_matchers") or []]
    parts = []
    for i, (name, op, value) in enumerate(matchers):
        if op not in NEGATED_OP:
            raise SyncError(f"通知策略中 continue 子路由的匹配运算符无法识别：{op}")
        parts.append([*matchers[:i], [name, NEGATED_OP[op], value]])
    return parts


def merge_policy_tree(tree: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Dispatch route first, other routes untouched, root email fallback last.

    The email routes take over exactly the akkiabot alerts that used to reach
    the root receiver: they come after every other route (so a matching route
    without `continue` still stops them) and exclude alerts matched by other
    routes with `continue`, which never reached the root either.
    """
    merged = copy.deepcopy(tree)
    root_receiver = merged.get("receiver")
    if not root_receiver:
        raise SyncError("notification policy 没有根 receiver")
    marker = list(config["marker"])
    others = [r for r in merged.get("routes") or [] if not _owned_route(r, marker)]
    dispatch = copy.deepcopy(config["dispatch_route"])
    dispatch["object_matchers"] = [*dispatch["object_matchers"], marker]
    base = [*config["email_route"]["object_matchers"], marker]
    combos: list[list[list[str]]] = [[]]
    for route in others:
        if route.get("continue"):
            combos = [c + part for c in combos for part in _not_matched_by(route)]
    emails = [{"receiver": root_receiver, "object_matchers": base + combo} for combo in combos]
    merged["routes"] = [dispatch, *others, *emails]
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
    # Read and validate everything before the first write, so a refused
    # policy tree leaves Grafana untouched.
    tree = merge_policy_tree(grafana.policies(), config)
    existing = {c.get("uid") for c in grafana.contact_points()}
    grafana.put_contact_point(contact_point, exists=contact_point["uid"] in existing)
    grafana.put_policies(tree)
    marker = list(config["marker"])
    owned = sum(_owned_route(r, marker) for r in tree["routes"])
    return [
        f"contact point {contact_point['uid']}: {'updated' if contact_point['uid'] in existing else 'created'}",
        f"notification policy: webhook route first, {owned - 1} email fallback route(s) last, "
        f"{len(tree['routes']) - owned} other routes and root receiver {tree['receiver']} kept",
    ]


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
    sub.add_parser(
        "notifications",
        help="可选：写入签名 webhook contact point 并合并通知策略路由（未配置地址时跳过）",
    )
    args = parser.parse_args(argv)
    if (
        args.command == "notifications"
        and not os.environ.get("GRAFANA_DISPATCH_WEBHOOK_URL", "").strip()
    ):
        print("notifications: GRAFANA_DISPATCH_WEBHOOK_URL not set, nothing changed")
        return 0
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

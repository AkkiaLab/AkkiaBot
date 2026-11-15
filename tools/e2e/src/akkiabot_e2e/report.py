"""Run report: JSON for machines, Markdown for issue comments. Always redacted."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from akkiabot_e2e.files import ensure_private_dir, write_private_file
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.scenarios import Status

Overall = Literal["passed", "failed", "skipped"]

_ICONS: dict[str, str] = {"passed": "✅", "failed": "❌", "error": "💥", "skipped": "⏭️"}

#: Why a scenario was skipped. A skipped scenario never counts as passed.
SKIP_REASONS: dict[str, str] = {
    "not_selected": "未选中（PR 级未改动）",
    "missing_credentials": "缺少凭据或配置",
    "rate_limited": "限流中止",
    "aborted": "运行中止",
}


@dataclass(slots=True)
class ScenarioResult:
    name: str
    milestone: str
    description: str
    status: Status
    detail: str = ""
    skip_reason: str | None = None
    """Key of :data:`SKIP_REASONS` when ``status == "skipped"``."""
    duration: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Report:
    run_id: str
    status: Overall
    started_at: str
    detail: str = ""
    chat: dict[str, Any] | None = None
    accounts: dict[str, Any] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)
    results: list[ScenarioResult] = field(default_factory=list)
    flood_wait_seconds: float = 0.0
    flood_waits: list[dict[str, Any]] = field(default_factory=list)
    """Every flood wait: time, account role, method, seconds, attempt (see ``pacing``)."""
    joins: dict[str, int] = field(default_factory=dict)
    """How the member got into the group this run: ``member_join_by_link`` (the
    joins Telegram rate-limits per account) and ``owner_added_member``."""
    summary: dict[str, int] = field(default_factory=dict)
    """Counts of ``passed`` / ``failed`` (incl. errors) / ``skipped`` results."""
    server_log: str | None = None
    release: dict[str, str | None] | None = None
    """Staging release run (``--record-status``): target sha and the Mini App
    versions observed at start and end of the run."""
    not_automated: list[str] = field(default_factory=list)
    """Steps the run could not perform automatically (e.g. a real Turnstile
    challenge). They are listed, never counted as passed."""

    def finish(self) -> None:
        """Overall status.

        Passed only if at least one scenario actually passed, none failed, and
        every skipped one was merely not selected (a PR-level run of the
        scenarios its change touches). A scenario skipped for missing
        credentials or a rate limit makes the run fail: it was wanted but not
        verified. ``--record-status`` is stricter still (``release.describe``).
        """
        self.summary = {
            "passed": sum(r.status == "passed" for r in self.results),
            "failed": sum(r.status in {"failed", "error"} for r in self.results),
            "skipped": sum(r.status == "skipped" for r in self.results),
        }
        if self.status == "skipped":
            return
        ok = self.summary["passed"] > 0 and all(
            r.status == "passed" or (r.status == "skipped" and r.skip_reason == "not_selected")
            for r in self.results
        )
        self.status = "passed" if ok and not self.detail else "failed"

    def fully_executed(self) -> bool:
        """Every scenario actually ran (none skipped for any reason)."""
        return bool(self.results) and all(r.status != "skipped" for r in self.results)

    def to_json(self, redact: Redactor) -> str:
        return redact(json.dumps(asdict(self), ensure_ascii=False, indent=2))

    def to_markdown(self, redact: Redactor) -> str:
        title = {"passed": "通过", "failed": "未通过", "skipped": "已跳过（未执行真实验收）"}
        lines = [f"### 真实 Telegram 验收：{title[self.status]}", ""]
        lines.append(f"- 运行 ID：`{self.run_id}`，开始时间 {self.started_at}")
        if self.chat:
            lines.append(f"- 测试群：{self.chat['title']}（`{self.chat['id']}`）")
        for role, info in self.accounts.items():
            lines.append(f"- {role}：{info['first_name']}（`{info['id']}`）")
        if self.setup:
            lines.append(f"- 准备步骤：{'；'.join(self.setup)}")
        if self.summary:
            lines.append(
                "- 汇总：实际通过 {passed} / 失败 {failed} / 跳过 {skipped}（不计为通过）".format(
                    **self.summary
                )
            )
        if self.joins:
            lines.append(
                f"- 本轮入群：成员通过链接入群 {self.joins.get('member_join_by_link', 0)} 次，"
                f"群主拉人 {self.joins.get('owner_added_member', 0)} 次"
            )
        if self.flood_wait_seconds:
            lines.append(f"- 因限流等待：{self.flood_wait_seconds:g} 秒")
        if self.joins and not self.flood_waits:
            lines.append("- FloodWait：本轮 0 次")
        if self.detail:
            lines.append(f"- 说明：{self.detail}")
        if self.release:
            lines.append(
                "- 发版验收版本：目标 `{target_sha}`，开始时 staging Mini App 为 "
                "`{webapp_sha_at_start}`，结束时为 `{webapp_sha_at_end}`".format(**self.release)
            )
        for item in self.not_automated:
            lines.append(f"- 未自动化（不计入通过）：{item}")
        if self.flood_waits:
            lines += [
                "",
                "| FloodWait 时间（UTC） | 账号 | 方法 | 秒数 | 第几次重试 | 处理 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for f in self.flood_waits:
                action = "超过上限，中止" if f["aborted"] else "等待后重试"
                lines.append(
                    f"| {f['at']} | {f['account']} | `{f['method']}` | {f['seconds']:g} | "
                    f"{f['attempt']} | {action} |"
                )
        if self.results:
            lines += ["", "| 场景 | 里程碑 | 结果 | 说明 |", "| --- | --- | --- | --- |"]
            for r in self.results:
                detail = r.detail.replace("|", "\\|").replace("\n", " ")
                if r.status == "skipped" and r.skip_reason:
                    reason = SKIP_REASONS.get(r.skip_reason, r.skip_reason)
                    detail = f"{reason}：{detail}" if detail else reason
                lines.append(
                    f"| `{r.name}` {r.description} | {r.milestone} | "
                    f"{_ICONS[r.status]} {r.status} | {detail} |"
                )
            lines += ["", "<details><summary>证据（消息原文）</summary>", ""]
            for r in self.results:
                if r.status == "skipped" and not r.evidence:
                    continue
                lines.append(f"**{r.name}**")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(r.evidence, ensure_ascii=False, indent=1))
                lines.append("```")
            lines.append("</details>")
        return redact("\n".join(lines) + "\n")

    def save(self, directory: Path, redact: Redactor) -> tuple[Path, Path]:
        ensure_private_dir(directory)
        json_path, md_path = directory / "report.json", directory / "report.md"
        write_private_file(json_path, self.to_json(redact), overwrite=True)
        write_private_file(md_path, self.to_markdown(redact), overwrite=True)
        return json_path, md_path

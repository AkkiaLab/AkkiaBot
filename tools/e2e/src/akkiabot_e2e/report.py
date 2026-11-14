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

_ICONS: dict[str, str] = {"passed": "✅", "failed": "❌", "error": "💥", "not_run": "⏭️"}


@dataclass(slots=True)
class ScenarioResult:
    name: str
    milestone: str
    description: str
    status: Status
    detail: str = ""
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
    server_log: str | None = None
    not_automated: list[str] = field(default_factory=list)
    """Steps the run could not perform automatically (e.g. a real Turnstile
    challenge). They are listed, never counted as passed."""

    def finish(self) -> None:
        """Overall status: passed only if at least one scenario ran and all passed."""
        if self.status == "skipped":
            return
        ok = bool(self.results) and all(r.status == "passed" for r in self.results)
        self.status = "passed" if ok and not self.detail else "failed"

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
        if self.flood_wait_seconds:
            lines.append(f"- 因限流等待：{self.flood_wait_seconds:g} 秒")
        if self.detail:
            lines.append(f"- 说明：{self.detail}")
        for item in self.not_automated:
            lines.append(f"- 未自动化（不计入通过）：{item}")
        if self.results:
            lines += ["", "| 场景 | 里程碑 | 结果 | 说明 |", "| --- | --- | --- | --- |"]
            for r in self.results:
                detail = r.detail.replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| `{r.name}` {r.description} | {r.milestone} | "
                    f"{_ICONS[r.status]} {r.status} | {detail} |"
                )
            lines += ["", "<details><summary>证据（消息原文）</summary>", ""]
            for r in self.results:
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

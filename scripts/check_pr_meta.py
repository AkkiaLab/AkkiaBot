"""Validate pull request metadata against the workspace conventions.

* The title must look like ``AKKIA-<number>: <summary>``.
* The body must contain ``Closes AKKIA-<number>`` with the same number.

The title and body are read from the event payload file named by
``GITHUB_EVENT_PATH`` (``pull_request.title`` / ``pull_request.body``). They are
deliberately NOT passed through step ``env``, the command line or the shell:
GitHub Actions prints a step's ``env`` block into the public job log, which
would publish the PR body verbatim — including text later edited out of the PR
(old workspace BOT-49). Messages printed by this script only describe the rule that failed and
never echo the title, the body or the payload.
"""

import json
import os
import re
import sys
from typing import Any

TITLE_RE = re.compile(r"^AKKIA-(?P<num>[1-9][0-9]*): \S.*$")
CLOSES_RE = re.compile(r"^\s*Closes AKKIA-(?P<num>[1-9][0-9]*)\b", re.MULTILINE)

EVENT_ERROR = (
    "无法读取 pull_request 事件（GITHUB_EVENT_PATH），本检查只能在 pull_request 触发的工作流中运行"
)


class EventError(Exception):
    """The event payload is missing or is not a pull_request event."""


def check(title: str, body: str) -> list[str]:
    """Return a list of human-readable problems; empty means the PR is valid.

    Messages describe rules only and never include ``title`` or ``body``.
    """
    errors: list[str] = []
    title_match = TITLE_RE.match(title.strip())
    if title_match is None:
        errors.append(
            "PR 标题必须形如 `AKKIA-<编号>: <中文摘要>`（大写 AKKIA、半角冒号后接空格与摘要）"
        )
    closes = {m.group("num") for m in CLOSES_RE.finditer(body)}
    if not closes:
        errors.append("PR 正文必须包含一行 `Closes AKKIA-<编号>`（建议放在第一行）")
    elif title_match is not None and title_match.group("num") not in closes:
        errors.append("PR 正文中 `Closes AKKIA-<编号>` 的编号与标题编号不一致，两者必须相同")
    return errors


def _text(value: Any) -> str:
    # GitHub sends ``"body": null`` for an empty description.
    return value if isinstance(value, str) else ""


def load_pull_request(path: str | None) -> tuple[str, str]:
    """Return ``(title, body)`` from a GitHub ``pull_request`` event payload file."""
    if not path:
        raise EventError
    try:
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise EventError from exc
    pr = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pr, dict):
        raise EventError
    return _text(pr.get("title")), _text(pr.get("body"))


def main() -> int:
    try:
        title, body = load_pull_request(os.environ.get("GITHUB_EVENT_PATH"))
    except EventError:
        print(f"::error::{EVENT_ERROR}")
        return 1
    errors = check(title, body)
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print("PR 标题与正文符合约定。")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

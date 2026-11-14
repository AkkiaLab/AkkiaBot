"""Validate pull request metadata against the workspace conventions.

* The title must look like ``BOT-<number>: <summary>``.
* The body must contain ``Closes BOT-<number>`` with the same number.

Reads ``PR_TITLE`` and ``PR_BODY`` from the environment (the workflow passes
them through ``env`` so untrusted text never reaches the shell) and exits
non-zero with a readable message when a rule is violated.
"""

import os
import re
import sys

TITLE_RE = re.compile(r"^BOT-(?P<num>[1-9][0-9]*): \S.*$")
CLOSES_RE = re.compile(r"^\s*Closes BOT-(?P<num>[1-9][0-9]*)\b", re.MULTILINE)


def check(title: str, body: str) -> list[str]:
    """Return a list of human-readable problems; empty means the PR is valid."""
    errors: list[str] = []
    title_match = TITLE_RE.match(title.strip())
    if title_match is None:
        errors.append(f"PR 标题必须形如 `BOT-<编号>: <中文摘要>`，当前为：{title!r}")
    closes = {m.group("num") for m in CLOSES_RE.finditer(body)}
    if not closes:
        errors.append("PR 正文必须包含一行 `Closes BOT-<编号>`（建议放在第一行）")
    elif title_match is not None and title_match.group("num") not in closes:
        errors.append(
            f"PR 正文中的 `Closes BOT-…` 编号 {sorted(closes)} "
            f"与标题编号 BOT-{title_match.group('num')} 不一致"
        )
    return errors


def main() -> int:
    errors = check(os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""))
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print("PR 标题与正文符合约定。")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

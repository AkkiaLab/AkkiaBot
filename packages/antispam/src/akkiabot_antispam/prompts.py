"""Prompts of the spam classifier.

The base prompt holds the role, task and few-shot examples. Each answer mode appends its own output instruction, so an
override only has to replace the base prompt and keeps working in both modes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from akkiabot_antispam.models import SpamContext

DEFAULT_PROMPT_VERSION = "antispam-v1"

DEFAULT_BASE_PROMPT = """角色： 你是一个善于识别广告、赌博、诈骗信息的智能助理
任务： 用户向你提供一个文本信息，你需要仔细识别是否是广告、赌博、诈骗信息。

案例1：
输入： 今天天气真好啊
结论： 不是

案例2：
输入： 看我主页日入2W
结论： 是（广告）

案例3：
输入： 不会搞薯条我教你，踏马的执行力拉满，别的不能给你，买一辆宝马五系还是没问题！
结论： 是（广告）

输入可能附带发送者的名字和简介，它们同样可以作为判断依据。"""

STRUCTURED_INSTRUCTION = """输出格式：
只输出一个 JSON 对象，不包含其他文字：
{"is_spam": true 或 false, "category": "ad" | "gambling" | "fraud" | "other" | "none", "confidence": 0 到 1 之间的小数}
category 含义：ad 广告引流，gambling 赌博，fraud 诈骗，other 其他垃圾信息，none 不是垃圾信息。"""

TEXT_INSTRUCTION = """输入输出格式：
输入： 一个文本信息
输出： 一个数字1或0用于代表识别结果，1代表信息属于上述类型，0代表不是。注意只输出一个数字，不包含文字提示。"""

VERDICT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_spam": {"type": "boolean"},
        "category": {"type": "string", "enum": ["ad", "gambling", "fraud", "other", "none"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["is_spam", "category", "confidence"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Prompt:
    """A base prompt and its version label (recorded with every call)."""

    base: str = DEFAULT_BASE_PROMPT
    version: str = DEFAULT_PROMPT_VERSION

    @classmethod
    def custom(cls, base: str, version: str | None = None) -> Prompt:
        """An overriding prompt; without an explicit version a content hash is used."""
        base = base.strip()
        if not base:
            raise ValueError("提示词不能为空")
        if version is None:
            version = "custom-" + hashlib.sha256(base.encode()).hexdigest()[:8]
        return cls(base, version)

    def system(self, *, structured: bool) -> str:
        instruction = STRUCTURED_INSTRUCTION if structured else TEXT_INSTRUCTION
        return f"{self.base}\n\n{instruction}"


def user_message(text: str, context: SpamContext | None = None) -> str:
    """The user turn: the text alone, or labelled lines when sender facts are known."""
    if context is None or (not context.sender_name and not context.sender_bio):
        return text
    lines = []
    if context.sender_name:
        lines.append(f"发送者名字: {context.sender_name}")
    if context.sender_bio:
        lines.append(f"发送者简介: {context.sender_bio}")
    lines.append(f"消息内容: {text}")
    return "\n".join(lines)

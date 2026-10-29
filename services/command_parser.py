"""调用语法识别：从原始消息解析正文与 @ 目标，并判定关键词路由。

优先解析 OneBot 原始段，避免框架加工的昵称与正文改变调用边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import astrbot.api.message_components as Comp

from ..utils.help import is_help_keyword
from ..utils.validation import match_command_syntax, parse_command_segments
from .onebot import REPLY_TYPES, cq_components

COMMAND_EVENT_KEY = "iconic_quotes_command_event"


@dataclass(slots=True, frozen=True)
class CallInput:
    text: str
    targets: list[str]


@dataclass(slots=True, frozen=True)
class Route:
    """一次已识别的插件调用：操作名与参数尾部。"""

    operation: str
    argument: str | None = None


def call_input(event: Any) -> CallInput:
    """返回调用正文与按出现顺序去重的 @ 目标。"""
    bot_id = str(event.get_self_id() or "")
    raw = getattr(event.message_obj, "raw_message", None)
    if isinstance(raw, dict) and isinstance(raw.get("message"), list):
        segments = raw["message"]
        if any(not isinstance(item, dict) for item in segments):
            return CallInput("", [])
        return CallInput(*parse_command_segments(segments, bot_id))
    if isinstance(raw, dict):
        raw_text = raw.get("message")
        if not isinstance(raw_text, str):
            raw_text = raw.get("raw_message")
        if not isinstance(raw_text, str):
            return CallInput("", [])
        components = cq_components(raw_text)
    else:
        components = event.get_messages()
    segments = []
    for item in components:
        if isinstance(item, Comp.Plain):
            segments.append({"type": "text", "data": {"text": item.text}})
        elif isinstance(item, Comp.At):
            segments.append({"type": "at", "data": {"qq": item.qq}})
        elif isinstance(item, REPLY_TYPES):
            segments.append({"type": "reply"})
        else:
            segments.append({"type": "other"})
    return CallInput(*parse_command_segments(segments, bot_id))


def capture_source_count(event: Any) -> int:
    """统计唯一收录来源，兼容 AstrBot 未展开的原始 reply 段。"""
    messages = list(event.get_messages())
    count = sum(
        isinstance(item, (Comp.Reply, Comp.Forward, Comp.Node, Comp.Nodes))
        for item in messages
    )
    if any(isinstance(item, Comp.Reply) for item in messages):
        return count
    raw = getattr(event.message_obj, "raw_message", None)
    raw_segments = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(raw_segments, list) and any(
        isinstance(item, dict) and item.get("type") == "reply" for item in raw_segments
    ):
        count += 1
    return count


def is_command_event(event: Any, wake_prefixes: Any) -> bool:
    """命令事件不再进入关键词监听，避免去前缀后的正文二次命中。"""
    if event.get_extra(COMMAND_EVENT_KEY, False):
        return True
    candidates = [str(getattr(event.message_obj, "message_str", "") or "")]
    raw = getattr(event.message_obj, "raw_message", None)
    if isinstance(raw, dict) and isinstance(raw.get("raw_message"), str):
        candidates.append(raw["raw_message"])
    if isinstance(wake_prefixes, str):
        prefixes = [wake_prefixes]
    elif isinstance(wake_prefixes, (list, tuple, set)):
        prefixes = list(wake_prefixes)
    else:
        prefixes = ["/"]
    return any(
        prefix and candidate.strip().startswith(str(prefix))
        for prefix in prefixes
        for candidate in candidates
    )


def is_bot_message(event: Any) -> bool:
    if str(event.get_sender_id()) == str(event.get_self_id()):
        return True
    raw = getattr(event.message_obj, "raw_message", None)
    sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
    return bool(sender.get("is_bot") or sender.get("is_robot"))


def route_query_command(call: CallInput, values: dict[str, Any]) -> Route | None:
    """`/群典` 指令：info、help 或随机查询；正文不完整匹配时不接管。"""
    matched, tail = match_command_syntax(call.text, ["群典"])
    if not matched:
        return None
    argument = tail or ""
    if not call.targets and argument.casefold() == "info":
        return Route("info")
    if not call.targets and is_help_keyword(argument, values["help_keywords"]):
        return Route("help")
    return Route("query", tail)


def route_keyword(
    call: CallInput, values: dict[str, Any], *, has_source: bool
) -> Route | None:
    """无前缀关键词：帮助 > 爆典 > 带来源的添加 > 查询 > 无来源的添加（给出用法）。"""
    if not call.text:
        return None
    add_match = values["add_keyword_enabled"] and call.text in values["add_keywords"]
    query_matched, query_tail = match_command_syntax(
        call.text, values["query_keywords"]
    )
    query_match = values["query_keyword_enabled"] and query_matched
    burst_matched, burst_tail = match_command_syntax(
        call.text, values["burst_keywords"]
    )
    if (
        query_match
        and not call.targets
        and is_help_keyword(query_tail, values["help_keywords"])
    ):
        return Route("help")
    if values["burst_keyword_enabled"] and burst_matched:
        return Route("burst", burst_tail)
    if add_match and has_source:
        return Route("add")
    if query_match:
        return Route("query", query_tail)
    if add_match:
        return Route("add")
    return None

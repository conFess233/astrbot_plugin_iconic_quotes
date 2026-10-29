"""OneBot 11 协议边界：接口调用、载荷解包、消息段解析与发送组件。"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Any

import astrbot.api.message_components as Comp


@dataclass(slots=True)
class RawReply:
    """OneBot reply 段；保留 NapCat 的消息序号。"""

    message_id: str
    message_seq: str | None = None


@dataclass(slots=True)
class RawSticker:
    """OneBot mface 商城表情段。"""

    file: str
    url: str
    emoji_package_id: str
    emoji_id: str
    key: str
    summary: str


@dataclass(slots=True)
class RawForward:
    """保留 forward 段的内联节点，避免转成组件时丢失 content。"""

    forward_id: str
    content: list[Any]


FORWARD_TYPES = (RawForward, Comp.Forward, Comp.Node, Comp.Nodes)
REPLY_TYPES = (Comp.Reply, RawReply)
# Node 段没有可读取的 ID，用哨兵交给统一的嵌套来源检查，不能静默忽略。
NESTED_NODE_SENTINEL = "__nested_node__"


def unwrap(value: Any) -> Any:
    """剥掉 OneBot 响应外层的 data；仅在入口调用一次。"""
    for _ in range(4):
        if not isinstance(value, dict) or "data" not in value:
            return value
        if any(key in value for key in ("message", "content", "messages", "nodes")):
            return value
        value = value["data"]
    return value


def _callers(event: Any) -> list[Any]:
    bot = getattr(event, "bot", None)
    return [
        caller
        for caller in (
            getattr(bot, "call_action", None),
            getattr(getattr(bot, "api", None), "call_action", None),
        )
        if callable(caller)
    ]


async def call_action(event: Any, action: str, **params: Any) -> Any:
    """读取类接口：兼容 aiocqhttp 两种 call_action 位置，失败时换入口重试。"""
    last_error: Exception | None = None
    for caller in _callers(event):
        try:
            return unwrap(await caller(action, **params))
        except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一。
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("当前 OneBot 适配器未提供 call_action")


async def call_action_once(event: Any, action: str, **params: Any) -> Any:
    """发送类接口：只调用一个入口，避免结果未知时换入口造成重复消息。"""
    callers = _callers(event)
    if not callers:
        raise RuntimeError("当前 OneBot 适配器未提供 call_action")
    return unwrap(await callers[0](action, **params))


def is_ambiguous_send_error(error: BaseException) -> bool:
    """网络中断和超时可能发生在服务端已受理之后，不能安全降级或换格式重发。"""
    return isinstance(
        error, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)
    )


# 入站消息段 -------------------------------------------------------------


def raw_component(value: dict[str, Any]) -> Any:
    """把一个 OneBot 段转换为 AstrBot 组件或本模块的原始段对象。"""
    kind = str(value.get("type") or "").lower()
    data = value.get("data") if isinstance(value.get("data"), dict) else {}
    if kind == "text":
        return Comp.Plain(str(data.get("text") or ""))
    if kind == "mface" or (kind == "image" and data.get("emoji_id")):
        return RawSticker(
            file=str(data.get("file") or data.get("url") or ""),
            url=str(data.get("url") or ""),
            emoji_package_id=str(data.get("emoji_package_id") or ""),
            emoji_id=str(data.get("emoji_id") or ""),
            key=str(data.get("key") or ""),
            summary=str(data.get("summary") or "[商城表情]"),
        )
    if kind == "image":
        return Comp.Image(
            file=str(data.get("file") or data.get("url") or ""),
            url=str(data.get("url") or ""),
        )
    if kind == "at":
        return Comp.At(qq=data.get("qq", ""), name=data.get("name", ""))
    if kind == "face":
        try:
            return Comp.Face(id=int(data.get("id")))
        except (TypeError, ValueError):
            return None
    if kind == "reply":
        return RawReply(
            str(data.get("id") or data.get("message_id") or ""),
            str(data.get("seq") or data.get("message_seq") or "") or None,
        )
    if kind == "forward":
        inline = data.get("content")
        return RawForward(
            forward_id=str(
                data.get("res_id") or data.get("resid") or data.get("id") or ""
            ),
            content=list(inline) if isinstance(inline, list) else [],
        )
    if kind == "node":
        return Comp.Forward(id=NESTED_NODE_SENTINEL)
    return None


_CQ_PATTERN = re.compile(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\]]*)?)\]")


def cq_components(value: str) -> list[Any]:
    """解析 CQ 字符串，未知段保留为普通文字。"""
    result: list[Any] = []
    cursor = 0
    for match in _CQ_PATTERN.finditer(value):
        if match.start() > cursor:
            result.append(Comp.Plain(html.unescape(value[cursor : match.start()])))
        data: dict[str, str] = {}
        raw_params = match.group(2).lstrip(",")
        for item in raw_params.split(",") if raw_params else []:
            key, separator, raw_value = item.partition("=")
            if separator:
                data[key] = html.unescape(raw_value)
        component = raw_component({"type": match.group(1), "data": data})
        result.append(component or Comp.Plain(html.unescape(match.group(0))))
        cursor = match.end()
    if cursor < len(value):
        result.append(Comp.Plain(html.unescape(value[cursor:])))
    return result


def segment_components(raw: Any) -> list[Any]:
    """消息内容（段列表或 CQ 字符串）转组件列表。"""
    if isinstance(raw, str):
        return cq_components(raw)
    if not isinstance(raw, list):
        return []
    return [
        component
        for item in raw
        if isinstance(item, dict)
        if (component := raw_component(item))
    ]


def payload_components(payload: dict[str, Any]) -> list[Any]:
    """get_msg 等接口返回的消息内容。"""
    raw = payload.get("message")
    if raw in (None, ""):
        raw = payload.get("content")
    return segment_components(raw)


def forward_payload_nodes(payload: Any) -> list[Any]:
    """get_forward_msg 返回的节点列表。"""
    payload = unwrap(payload)
    if isinstance(payload, dict):
        raw = payload.get("messages") or payload.get("message") or payload.get("nodes")
        if isinstance(raw, list):
            return raw
    return []


def raw_reply_data(event: Any) -> dict[str, Any]:
    """从原始事件中补取引用 ID 与消息序号。"""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, dict):
        return {}
    message = raw.get("message")
    if isinstance(message, list):
        for item in message:
            if isinstance(item, dict) and item.get("type") == "reply":
                data = item.get("data")
                return dict(data) if isinstance(data, dict) else {}
    raw_text = raw.get("raw_message")
    if isinstance(raw_text, str):
        for component in cq_components(raw_text):
            if isinstance(component, RawReply):
                return {"id": component.message_id, "seq": component.message_seq}
    return {}


def member_name(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return str(payload.get("card") or payload.get("nickname") or "").strip() or None


# 出站组件 --------------------------------------------------------------


class OneBotReply(Comp.Reply):
    """为 NapCat 保留原生消息序号，同时兼容标准 OneBot 的消息 ID。"""

    def toDict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": str(self.id)}
        if self.seq not in (None, 0, "0", ""):
            data["seq"] = str(self.seq)
        return {"type": "reply", "data": data}


class MarketFaceComponent(Comp.BaseMessageComponent):
    """AstrBot 尚未内建的 OneBot mface 发送组件。"""

    type: Comp.ComponentType = Comp.ComponentType.Image
    emoji_package_id: str
    emoji_id: str
    key: str
    summary: str

    def toDict(self) -> dict[str, Any]:
        return {
            "type": "mface",
            "data": {
                "emoji_package_id": self.emoji_package_id,
                "emoji_id": self.emoji_id,
                "key": self.key,
                "summary": self.summary,
            },
        }

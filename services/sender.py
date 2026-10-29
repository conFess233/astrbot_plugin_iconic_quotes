"""消息发送：有限重试、结果未知判定与“回复 / 贴纸 / 时间模式”降级。

明确失败可以降级或换格式；超时、断线等结果未知的失败可能已经送达，
默认立即停止，不再重试或降级，避免重复消息。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain

from ..models import QuoteRecord
from ..utils.timefmt import error_reason
from .forward_builder import BuildOptions, ForwardBuilder, Raw, forward_meta
from .onebot import call_action_once, is_ambiguous_send_error

Outcome = Literal["success", "explicit", "ambiguous"]
# 由 BuildOptions 生成一次发送的节点树。
TreeBuilder = Callable[[BuildOptions], list[Raw]]


class AmbiguousSendError(RuntimeError):
    """发送可能已经送达，不能继续切换格式或创建删除确认。"""


@dataclass(slots=True)
class OperationContext:
    """一次插件操作的配置快照与发送结果，显式传递而不挂在插件实例上。"""

    values: dict[str, Any]
    sent: bool = False
    log_override: str | None = None
    error_logged: bool = False
    missing: frozenset[str] = field(default_factory=frozenset)


def fallback_variants(
    *, has_replies: bool, has_stickers: bool, time_modes: tuple[str, ...] = ("text",)
) -> list[tuple[bool, bool, str]]:
    """(原生回复, 原生贴纸, 时间模式) 的尝试顺序：先降时间模式，再降回复，最后降贴纸。"""
    variants = [(True, True, mode) for mode in time_modes]
    if has_replies:
        variants += [(False, True, mode) for mode in time_modes]
    if has_stickers:
        variants += [(replies, False, mode) for replies, _, mode in list(variants)]
    return list(dict.fromkeys(variants))


def time_modes(configured: str) -> tuple[str, ...]:
    """实验性原生时间失败时退回不显示时间。"""
    return (configured, "none") if configured == "native" else (configured,)


class Sender:
    """封装 event.send 与 send_group_forward_msg 的重试与降级。"""

    def __init__(self, builder: ForwardBuilder):
        self.builder = builder

    # 基础发送 ---------------------------------------------------------

    async def chain(self, event: Any, ctx: OperationContext, chain: list[Any]) -> None:
        values = ctx.values
        attempts = values["send_retry_count"] + 1
        delay = values["send_retry_delay_ms"] / 1000
        last_error: Exception | None = None
        saw_ambiguous = False
        for attempt in range(attempts):
            try:
                await event.send(MessageChain(chain))
                ctx.sent = True
                return
            except Exception as exc:  # noqa: BLE001 - OneBot 适配器异常类型不稳定。
                last_error = exc
                ambiguous = is_ambiguous_send_error(exc)
                saw_ambiguous = saw_ambiguous or ambiguous
                if ambiguous and not values["retry_on_ambiguous_failure"]:
                    break
            if attempt + 1 < attempts:
                logger.debug(
                    "群典：消息发送失败，准备重试；尝试=%s/%s，消息段数=%s，原因=%s",
                    attempt + 1,
                    attempts,
                    len(chain),
                    error_reason(last_error),
                )
                await asyncio.sleep(delay)
        assert last_error is not None
        if saw_ambiguous:
            raise AmbiguousSendError("消息可能已送达") from last_error
        raise last_error

    async def text(self, event: Any, ctx: OperationContext, text: str) -> None:
        await self.chain(event, ctx, [Comp.Plain(text)])

    @staticmethod
    async def notice(event: Any, text: str) -> bool:
        """冷却或平台提示：不重试，失败也不抛出。"""
        try:
            await event.send(MessageChain([Comp.Plain(text)]))
            return True
        except Exception:  # noqa: BLE001 - 提示失败不能递归重试。
            return False

    async def raw_forward(
        self, event: Any, ctx: OperationContext, messages: list[Raw]
    ) -> tuple[Outcome, Exception | None]:
        """直接发送节点树，并区分明确失败与结果未知。"""
        values = ctx.values
        attempts = values["send_retry_count"] + 1
        delay = values["send_retry_delay_ms"] / 1000
        last_error: Exception | None = None
        last: Outcome = "explicit"
        saw_ambiguous = False
        params: dict[str, Any] = {
            "group_id": int(event.get_group_id()),
            "messages": messages,
            **forward_meta(messages),
        }
        self_id = str(event.get_self_id() or "")
        if self_id.isdigit():
            params["self_id"] = int(self_id)
        for attempt in range(attempts):
            try:
                await call_action_once(event, "send_group_forward_msg", **params)
                ctx.sent = True
                return "success", None
            except Exception as exc:  # noqa: BLE001 - OneBot 错误类型不统一。
                last_error = exc
                last = "ambiguous" if is_ambiguous_send_error(exc) else "explicit"
                saw_ambiguous = saw_ambiguous or last == "ambiguous"
                if last == "ambiguous" and not values["retry_on_ambiguous_failure"]:
                    logger.warning(
                        "群典：合并转发结果未知，停止重试；尝试=%s/%s，节点数=%s，原因=%s",
                        attempt + 1,
                        attempts,
                        len(messages),
                        error_reason(exc),
                    )
                    break
            if attempt + 1 < attempts:
                logger.debug(
                    "群典：合并转发发送失败，准备重试；尝试=%s/%s，结果=%s，原因=%s",
                    attempt + 1,
                    attempts,
                    last,
                    error_reason(last_error),
                )
                await asyncio.sleep(delay)
        # 任一次结果未知都意味着消息可能已经送达，后续明确失败不能消除重复风险。
        return ("ambiguous" if saw_ambiguous else last), last_error

    # 降级流程 ---------------------------------------------------------

    async def _try_variants(
        self,
        event: Any,
        ctx: OperationContext,
        build: TreeBuilder,
        variants: list[tuple[bool, bool, str]],
        *,
        flatten: bool,
    ) -> tuple[Outcome, Exception | None]:
        last_error: Exception | None = None
        for native_replies, native_stickers, mode in variants:
            options = BuildOptions(
                native_replies=native_replies,
                native_stickers=native_stickers,
                flatten=flatten,
                time_mode=mode,
                missing=ctx.missing,
            )
            try:
                messages = await asyncio.to_thread(build, options)
            except Exception as exc:  # noqa: BLE001 - 本地序列化失败属于明确失败。
                last_error = exc
                logger.debug(
                    "群典：构造合并转发失败；%s，原因=%s", options, error_reason(exc)
                )
                continue
            outcome, error = await self.raw_forward(event, ctx, messages)
            if outcome != "explicit":
                return outcome, error
            last_error = error
            logger.debug(
                "群典：合并转发发送失败；%s，原因=%s", options, error_reason(error)
            )
        return "explicit", last_error

    async def forward(
        self,
        event: Any,
        ctx: OperationContext,
        build: TreeBuilder,
        *,
        depth: int,
        has_replies: bool,
        has_stickers: bool,
        configured_time_mode: str = "text",
        failure_text: str = "合并转发发送失败，请稍后重试。",
        label: str = "",
    ) -> bool:
        """原生节点树优先；明确失败后提示并完整展开；结果未知时停止。"""
        values = ctx.values
        variants = fallback_variants(
            has_replies=has_replies,
            has_stickers=has_stickers,
            time_modes=time_modes(configured_time_mode),
        )
        # OneBot 实现对超过 3 层的节点树普遍不可靠，本地预检后直接展开。
        if depth <= 3:
            outcome, error = await self._try_variants(
                event, ctx, build, variants, flatten=False
            )
            if outcome == "success":
                ctx.log_override = "原生合并转发"
                return True
            if outcome == "ambiguous":
                await self.text(event, ctx, values["nested_forward_unknown_message"])
                ctx.log_override = "合并转发结果未知"
                return False
            logger.warning(
                "群典：原生合并转发失败，准备降级；%s节点树深度=%s，原因=%s",
                label,
                depth,
                error_reason(error),
            )
        else:
            logger.warning(
                "群典：原生合并转发未发送，准备降级；%s节点树深度=%s，原因=超过 OneBot 安全深度 3",
                label,
                depth,
            )
        await self.text(event, ctx, values["nested_forward_fallback_message"])
        outcome, error = await self._try_variants(
            event, ctx, build, variants, flatten=True
        )
        if outcome == "success":
            ctx.log_override = "合并转发失败，已降级"
            return True
        if outcome == "ambiguous":
            raise AmbiguousSendError("消息可能已送达") from error
        ctx.error_logged = True
        logger.error(
            "群典：合并转发降级发送失败；%s原因=%s",
            label,
            error_reason(error),
            exc_info=(type(error), error, error.__traceback__) if error else None,
        )
        await self.text(event, ctx, failure_text)
        return False

    async def record_chain(
        self, event: Any, ctx: OperationContext, record: QuoteRecord
    ) -> None:
        """单条普通记录：原生链优先，贴纸降为图片；回复不可用时拆为转发节点。"""
        sticker_modes = [True, False] if record.has_stickers() else [True]
        last_error: Exception | None = None
        for native_stickers in sticker_modes:
            options = BuildOptions(native_stickers=native_stickers, missing=ctx.missing)
            try:
                chain = await asyncio.to_thread(
                    self.builder.text_chain, record, options
                )
                await self.chain(event, ctx, chain)
                return
            except AmbiguousSendError:
                raise
            except Exception as exc:  # noqa: BLE001 - 适配器拒绝原生段时继续降级。
                last_error = exc
                if record.reply:
                    break
        if record.reply:
            for native_stickers in sticker_modes:
                options = BuildOptions(
                    native_replies=False,
                    native_stickers=native_stickers,
                    flatten=True,
                    missing=ctx.missing,
                )
                messages = await asyncio.to_thread(
                    self.builder.forward, [record], options, replay=False
                )
                outcome, error = await self.raw_forward(event, ctx, messages)
                if outcome == "success":
                    ctx.log_override = "已降级发送"
                    return
                if outcome == "ambiguous":
                    raise AmbiguousSendError("消息可能已送达") from error
                last_error = error
        if last_error:
            raise last_error

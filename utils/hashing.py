"""群典内容规范化与稳定哈希。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from ..models import AuthorSnapshot, ForwardNode, QuoteRecord, QuoteSegment


def normalize_text(value: str) -> str:
    """规范化判重文本，同时保留有意义的内部空白。"""
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n")
    normalized = normalized.replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines).strip()


def normalize_search(value: str) -> str:
    """规范化不区分大小写的删除搜索文本。"""
    return normalize_text(value).casefold()


def _author_key(author: AuthorSnapshot | None) -> dict[str, str | None]:
    if author is None:
        return {"user_id": None, "nickname": ""}
    return {
        "user_id": author.user_id,
        "nickname": "" if author.user_id else normalize_text(author.nickname),
    }


def _segment_key(segment: QuoteSegment) -> dict[str, Any]:
    if segment.type == "text":
        return {"type": "text", "text": normalize_text(segment.text or "")}
    return {"type": "image", "sha256": segment.sha256 or ""}


def _node_key(node: ForwardNode) -> dict[str, Any]:
    return {
        "author": _author_key(node.author),
        "segments": [_segment_key(segment) for segment in node.segments],
    }


def calculate_record_hash(record: QuoteRecord) -> str:
    """计算排除时间、操作者和昵称变化的稳定内容哈希。"""
    payload: dict[str, Any] = {
        "type": record.type,
        "author": _author_key(record.author),
        "segments": [_segment_key(segment) for segment in record.segments],
        "nodes": [_node_key(node) for node in record.nodes],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

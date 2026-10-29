"""群典持久化模型。"""

from .quote import (
    AuthorSnapshot,
    ForwardNode,
    NestedForward,
    QuoteRecord,
    QuoteSegment,
    ReplySnapshot,
    remove_forward_node,
)

__all__ = [
    "AuthorSnapshot",
    "ForwardNode",
    "NestedForward",
    "QuoteRecord",
    "QuoteSegment",
    "ReplySnapshot",
    "remove_forward_node",
]

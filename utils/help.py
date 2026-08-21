"""帮助关键词匹配与模板渲染。"""

from __future__ import annotations

from typing import Any


def is_help_keyword(value: str | None, keywords: list[str]) -> bool:
    """按忽略大小写的完整值匹配帮助子关键词。"""
    candidate = str(value or "").strip().casefold()
    return bool(candidate) and candidate in {
        str(keyword).strip().casefold() for keyword in keywords
    }


def render_help_template(template: str, values: dict[str, Any]) -> str:
    """渲染帮助占位符，并移除已关闭关键词对应的提示片段。"""
    rendered = str(template or "").strip()
    keyword_fields = (
        ("add_keywords", "add_keyword_enabled"),
        ("query_keywords", "query_keyword_enabled"),
        ("burst_keywords", "burst_keyword_enabled"),
    )
    for field, enabled_key in keyword_fields:
        if values[enabled_key]:
            continue
        token = "{" + field + "}"
        for phrase in (
            f"；关键词：{token}",
            f";关键词：{token}",
            f"；关键词: {token}",
            f"; keywords: {token}",
            f"关键词：{token}",
            token,
        ):
            rendered = rendered.replace(phrase, "")
    context = {
        "add_keywords": "、".join(values["add_keywords"]),
        "query_keywords": "、".join(values["query_keywords"]),
        "burst_keywords": "、".join(values["burst_keywords"]),
        "burst_page_size": values["burst_page_size"],
        "max_query_keyword_chars": values["max_query_keyword_chars"],
    }
    return "\n".join(
        line.rstrip("；;，, ")
        for line in rendered.format_map(context).splitlines()
        if line.strip()
    ).strip()


def operation_usage(values: dict[str, Any], operation: str) -> str:
    """读取并渲染指定操作的帮助模板。"""
    return render_help_template(values.get(f"help_{operation}_template", ""), values)

"""帮助关键词匹配与模板渲染。"""

from __future__ import annotations

import html
import re
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


HELP_FOOTER = (
    "支持 @成员 群典 或 群典 @成员；关键词前最多一个成员 @，也可引用消息或 @Bot。"
)
HELP_SECTIONS = (
    ("add", "添加"),
    ("query", "查询"),
    ("burst", "爆典"),
    ("info", "统计"),
    ("delete", "删除"),
    ("help", "帮助"),
)


def build_help_text(values: dict[str, Any]) -> str:
    """当前配置下的完整帮助：总模板留空时组合各操作用法，末尾附调用说明。"""
    overview = render_help_template(values["help_overview_template"], values)
    if not overview:
        sections = [
            f"{label}：{usage}"
            for operation, label in HELP_SECTIONS
            if (usage := operation_usage(values, operation))
        ]
        overview = "群典帮助" + ("\n" + "\n".join(sections) if sections else "")
    return f"{overview}\n{HELP_FOOTER}"


# 指令本身及紧随的英文子命令（如 /群典 info）。
_COMMAND = re.compile(r"/[^\s，、；;,。<>&]+(?: [A-Za-z]+\b)?")
_ARGUMENT = re.compile(r"&lt;[^&]*?&gt;|\[[^\]]+\]")
_KEYWORDS = re.compile(r"[；;]?\s*关键词[：:]\s*")


def help_card_data(overview: str) -> dict[str, Any]:
    """把渲染后的帮助文本拆成卡片数据：标题、按操作分行的用法与补充说明。

    “标签：用法；关键词：甲、乙”的行成为一行用法，指令与参数加上标记；
    其余行作为补充说明。返回值中的文本均已做 HTML 转义。
    """
    lines = [line.strip() for line in overview.splitlines() if line.strip()]
    title = "群典帮助"
    if lines and "：" not in lines[0] and ":" not in lines[0]:
        title = lines.pop(0)
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for line in lines:
        label, separator, rest = line.partition("：")
        if not separator or not label or len(label) > 6:
            notes.append(html.escape(line))
            continue
        parts = _KEYWORDS.split(rest, maxsplit=1)
        body = parts[0].rstrip("；; ")
        words = (
            [word.strip() for word in parts[1].split("、")] if len(parts) > 1 else []
        )
        markup = _COMMAND.sub(
            lambda match: f"<code>{match.group(0)}</code>", html.escape(body)
        )
        markup = _ARGUMENT.sub(
            lambda match: f'<i class="arg">{match.group(0)}</i>', markup
        )
        rows.append(
            {
                "label": html.escape(label),
                "body": markup,
                "keywords": [html.escape(word) for word in words if word],
            }
        )
    return {"title": html.escape(title), "rows": rows, "notes": notes}

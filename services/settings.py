"""插件配置校验、默认值与群级覆盖。"""

from __future__ import annotations

import copy
from typing import Any

from ..utils.hashing import normalize_search
from ..utils.validation import sanitize_custom_css, validate_numeric_id

ROLE_VALUES = {"bot_admin", "owner", "admin", "member", "everyone"}
GROUP_OVERRIDE_KEYS = {
    "add_keyword_enabled",
    "add_keywords",
    "query_keyword_enabled",
    "query_keywords",
    "max_query_keyword_chars",
    "burst_keyword_enabled",
    "burst_keywords",
    "burst_page_size",
    "burst_time_mode",
    "send_count",
    "random_send_count",
    "send_mode",
    "aggregate_multiple",
    "allow_bot_authors",
    "max_records_per_group",
    "add_roles",
    "query_roles",
    "burst_roles",
    "info_roles",
    "delete_roles",
    "user_blacklist",
    "user_whitelist",
    "excluded_author_ids",
    "nested_forward_fallback_message",
    "nested_forward_unknown_message",
}


DEFAULTS: dict[str, Any] = {
    "storage_subdir": "iconic_quotes",
    "add_keyword_enabled": True,
    "add_keywords": ["添加群典"],
    "query_keyword_enabled": True,
    "query_keywords": ["群典"],
    "max_query_keyword_chars": 100,
    "burst_keyword_enabled": True,
    "burst_keywords": ["爆典"],
    "burst_page_size": 50,
    "burst_time_mode": "text",
    "max_reply_depth": 3,
    "max_nested_forward_depth": 3,
    "max_records_per_group": 5000,
    "max_media_mb": 2048,
    "max_image_mb": 10,
    "max_images_per_record": 9,
    "max_forward_nodes": 100,
    "max_text_chars": 5000,
    "max_forward_text_chars": 50000,
    "send_count": 1,
    "random_send_count": False,
    "send_mode": "text",
    "aggregate_multiple": True,
    "allow_bot_authors": False,
    "add_roles": ["everyone"],
    "query_roles": ["everyone"],
    "burst_roles": ["member"],
    "info_roles": ["everyone"],
    "delete_roles": ["bot_admin"],
    "group_blacklist": [],
    "group_whitelist": [],
    "user_blacklist": [],
    "user_whitelist": [],
    "excluded_author_ids": [],
    "group_overrides": {},
    # 作者别名只用于合并转发节点和回复快照，不会改写普通消息作者。
    "author_aliases": {},
    "global_cooldown_ms": 1000,
    "cooldown_message": "群典功能冷却中...",
    "delete_preview_limit": 20,
    "send_retry_count": 2,
    "send_retry_delay_ms": 500,
    "retry_on_ambiguous_failure": False,
    "nested_forward_fallback_message": "原生多层嵌套发送失败，以下内容已降级展开。",
    "nested_forward_unknown_message": "多层群典发送结果未知，为避免重复发送，本次未执行降级，请检查群消息。",
    "audit_limit": 10000,
    "card_width": 1200,
    "card_min_height": 480,
    "card_max_height": 2000,
    "card_custom_css": "",
    "card_auto_height": True,
    "localize_avatars": False,
    "avatar_cache_ttl_days": 7,
}


class SettingsService:
    """提供经过边界收敛的配置快照。"""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def global_settings(self) -> dict[str, Any]:
        """返回带默认值且完成校验的全局配置。"""
        merged = copy.deepcopy(DEFAULTS)
        merged.update(dict(self.config))
        return self._validate(merged)

    def for_group(self, group_id: str) -> dict[str, Any]:
        """在全局默认之上应用当前群覆盖。"""
        merged = self.global_settings()
        overrides = merged.get("group_overrides")
        if isinstance(overrides, dict):
            override = overrides.get(str(group_id))
            if isinstance(override, dict):
                merged.update(
                    {
                        key: value
                        for key, value in override.items()
                        if key in GROUP_OVERRIDE_KEYS
                    }
                )
        return self._validate(merged)

    def update_from_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验页面提交的完整或部分配置并写回内存对象。"""
        candidate = copy.deepcopy(dict(self.config))
        candidate.update(payload)
        validated = self._validate({**DEFAULTS, **candidate})
        self.config.update({key: validated[key] for key in DEFAULTS})
        return validated

    @staticmethod
    def _id_list(value: Any, label: str) -> list[str]:
        if not isinstance(value, list):
            raise TypeError(f"{label} 必须是列表")
        return sorted({validate_numeric_id(item, label) for item in value})

    @staticmethod
    def _roles(value: Any, label: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError(f"{label} 至少选择一个角色")
        roles = [str(item) for item in value]
        if any(role not in ROLE_VALUES for role in roles):
            raise ValueError(f"{label} 包含未知角色")
        return list(dict.fromkeys(roles))

    @staticmethod
    def _author_aliases(value: Any) -> dict[str, dict[str, list[str]]]:
        """校验群级作者别名，并阻止同一规范化别名指向多个 QQ。"""
        if not isinstance(value, dict):
            raise TypeError("author_aliases 必须是对象")
        cleaned: dict[str, dict[str, list[str]]] = {}
        for raw_group_id, raw_targets in value.items():
            group_id = validate_numeric_id(raw_group_id, "作者别名群号")
            if not isinstance(raw_targets, dict):
                raise TypeError(f"群 {group_id} 的作者别名必须是对象")
            owners: dict[str, str] = {}
            targets: dict[str, list[str]] = {}
            for raw_user_id, raw_aliases in raw_targets.items():
                user_id = validate_numeric_id(raw_user_id, "作者别名 QQ")
                if not isinstance(raw_aliases, list):
                    raise TypeError(f"QQ {user_id} 的作者别名必须是列表")
                aliases: list[str] = []
                for raw_alias in raw_aliases:
                    alias = str(raw_alias).strip()
                    if not alias or len(alias) > 100:
                        raise ValueError("作者别名不能为空且不能超过 100 个字符")
                    normalized = normalize_search(alias)
                    owner = owners.get(normalized)
                    if owner and owner != user_id:
                        raise ValueError(
                            f"群 {group_id} 的别名“{alias}”不能同时绑定多个 QQ"
                        )
                    owners[normalized] = user_id
                    if alias not in aliases:
                        aliases.append(alias)
                if aliases:
                    targets[user_id] = aliases
            if targets:
                cleaned[group_id] = targets
        return cleaned

    @classmethod
    def _validate(
        cls,
        value: dict[str, Any],
        *,
        validate_overrides: bool = True,
    ) -> dict[str, Any]:
        result = copy.deepcopy(value)
        integer_ranges = {
            "max_records_per_group": (1, 100_000),
            "max_media_mb": (1, 102_400),
            "max_image_mb": (1, 100),
            "max_images_per_record": (1, 100),
            "max_forward_nodes": (1, 200),
            "max_text_chars": (1, 100_000),
            "max_forward_text_chars": (1, 1_000_000),
            "max_query_keyword_chars": (1, 1000),
            "send_count": (1, 10),
            "burst_page_size": (1, 100),
            "max_reply_depth": (1, 10),
            "max_nested_forward_depth": (1, 10),
            "global_cooldown_ms": (0, 60_000),
            "delete_preview_limit": (1, 50),
            "send_retry_count": (0, 5),
            "send_retry_delay_ms": (100, 10_000),
            "audit_limit": (1, 100_000),
            "card_width": (480, 3000),
            "card_min_height": (240, 3000),
            "card_max_height": (480, 6000),
            "avatar_cache_ttl_days": (0, 3650),
        }
        for key, (minimum, maximum) in integer_ranges.items():
            try:
                number = int(result[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是整数") from exc
            if not minimum <= number <= maximum:
                raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
            result[key] = number
        if result["card_min_height"] > result["card_max_height"]:
            raise ValueError("卡片最小高度不能大于最大高度")
        if result.get("send_mode") not in {"text", "card"}:
            raise ValueError("send_mode 只能是 text 或 card")
        if result.get("burst_time_mode") not in {"text", "native", "none"}:
            raise ValueError("burst_time_mode 只能是 text、native 或 none")
        for key in (
            "add_keyword_enabled",
            "query_keyword_enabled",
            "burst_keyword_enabled",
            "aggregate_multiple",
            "random_send_count",
            "allow_bot_authors",
            "retry_on_ambiguous_failure",
            "card_auto_height",
            "localize_avatars",
        ):
            if not isinstance(result.get(key), bool):
                raise TypeError(f"{key} 必须是布尔值")
        for key in ("add_keywords", "query_keywords", "burst_keywords"):
            raw = result.get(key)
            if not isinstance(raw, list):
                raise TypeError(f"{key} 必须是列表")
            cleaned = [str(item).strip() for item in raw if str(item).strip()]
            result[key] = list(dict.fromkeys(cleaned))
        for key in (
            "add_roles",
            "query_roles",
            "burst_roles",
            "info_roles",
            "delete_roles",
        ):
            result[key] = cls._roles(result.get(key), key)
        for key in (
            "group_blacklist",
            "group_whitelist",
            "user_blacklist",
            "user_whitelist",
            "excluded_author_ids",
        ):
            result[key] = cls._id_list(result.get(key, []), key)
        if not isinstance(result.get("group_overrides"), dict):
            raise TypeError("group_overrides 必须是对象")
        if validate_overrides:
            cleaned_overrides: dict[str, dict[str, Any]] = {}
            base = copy.deepcopy(result)
            base["group_overrides"] = {}
            for raw_group_id, raw_override in result["group_overrides"].items():
                group_id = validate_numeric_id(raw_group_id, "群级覆盖群号")
                if not isinstance(raw_override, dict):
                    raise TypeError(f"群 {group_id} 的覆盖配置必须是对象")
                unknown = set(raw_override) - GROUP_OVERRIDE_KEYS
                if unknown:
                    raise ValueError(
                        f"群 {group_id} 包含不允许覆盖的配置项: "
                        + ", ".join(sorted(unknown))
                    )
                candidate = copy.deepcopy(base)
                candidate.update(raw_override)
                validated = cls._validate(candidate, validate_overrides=False)
                cleaned_overrides[group_id] = {
                    key: validated[key] for key in raw_override
                }
            result["group_overrides"] = cleaned_overrides
        result["author_aliases"] = cls._author_aliases(result.get("author_aliases", {}))
        result["card_custom_css"] = sanitize_custom_css(
            str(result.get("card_custom_css") or "")
        )
        result["cooldown_message"] = str(result.get("cooldown_message") or "").strip()
        if not result["cooldown_message"]:
            raise ValueError("冷却提示不能为空")
        for key, label in (
            ("nested_forward_fallback_message", "多层嵌套降级提示"),
            ("nested_forward_unknown_message", "多层嵌套结果未知提示"),
        ):
            result[key] = str(result.get(key) or "").strip()
            if not result[key] or len(result[key]) > 500:
                raise ValueError(f"{label}不能为空且不能超过 500 个字符")
        return result

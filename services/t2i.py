"""文转图（t2i）渲染：使用 AstrBot 的文转图设置，或直接调用插件单独配置的服务。

单独配置时请求 astrbot-t2i-service 的 ``POST <端点>/text2img/generate``，
可指向本地部署（如 ``http://127.0.0.1:8999``），卡片数据不经过公共服务。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

# Star.html_render(tmpl, data, return_url=..., options=...)
HtmlRender = Callable[..., Awaitable[str]]
MAX_IMAGE_BYTES = 30 * 1024 * 1024


class T2IError(RuntimeError):
    """文转图服务明确失败：调用方降级为文字发送。"""


def normalize_endpoint(value: str) -> str:
    """补全为 ``…/text2img``，与 AstrBot 的端点写法保持一致。"""
    url = value.strip().removesuffix("/")
    return url if url.endswith("/text2img") else f"{url}/text2img"


class T2IClient:
    """渲染 Jinja 模板为 PNG 临时文件；调用方负责删除返回的文件。"""

    def __init__(self, html_render: HtmlRender, temp_dir: Path):
        self._html_render = html_render
        self.temp_dir = temp_dir
        self._http: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    async def render(
        self, template: str, data: dict[str, Any], settings: dict[str, Any]
    ) -> str:
        # 视口宽度等于卡片宽度、高度取最小值，整页截图即为卡片本身，无需再裁切留白。
        options = {
            "type": "png",
            "full_page": True,
            "viewport_width": int(data["width"]),
            "viewport_height": 120,
            "device_scale_factor_level": settings["card_scale"],
        }
        endpoint = settings["card_t2i_endpoint"]
        if not endpoint:
            return await self._html_render(
                template, data, return_url=False, options=options
            )
        return await self._request(
            normalize_endpoint(endpoint),
            {"tmpl": template, "tmpldata": data, "json": False, "options": options},
            timeout=settings["card_t2i_timeout_s"],
        )

    async def _request(
        self, endpoint: str, payload: dict[str, Any], *, timeout: int
    ) -> str:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        try:
            async with self._http.post(
                f"{endpoint}/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:200].strip()
                    raise T2IError(f"文转图服务返回 HTTP {response.status}：{detail}")
                if not response.content_type.startswith("image/"):
                    raise T2IError(
                        f"文转图服务返回了非图片内容：{response.content_type}"
                    )
                data = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise T2IError(f"无法连接文转图服务 {endpoint}：{exc}") from exc
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise T2IError("文转图服务返回的图片为空或过大")
        return await asyncio.to_thread(self._write_temp, data)

    def _write_temp(self, data: bytes) -> str:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=self.temp_dir, prefix="card-", suffix=".png")
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        return path

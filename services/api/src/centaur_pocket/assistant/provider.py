"""Model providers for the assistant loop (§3.4).

模型编排跑在 Pocket 服务端：手机端绝不持有模型密钥。本地档默认用 Ollama，
云端档用 Anthropic Messages API（raw HTTP，与本项目 urllib 惯例一致）。
两个 provider 把各自的线上格式归一成中立的会话表示，循环层不感知差异。

中立会话格式（loop 拥有，provider 各自转译）：

- ``{"role": "user", "content": str}``
- ``{"role": "assistant", "content": str, "tool_calls": [ToolCall...]}``
- ``{"role": "tool", "call_id": str, "name": str, "content": str}``
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..service import new_id


class ProviderError(RuntimeError):
    """Provider 不可达或返回无法解析的内容；客户端据此走 §3.5 降级通路。"""


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ProviderReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001 - 读失败时保留状态码即可
            pass
        raise ProviderError(f"HTTP {error.code}: {detail or error.reason}") from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise ProviderError(str(error)) from error
    if not isinstance(decoded, dict):
        raise ProviderError("响应不是 JSON 对象")
    return decoded


class OllamaProvider:
    """本地档：Ollama /api/chat，非流式，原生 tools 协议。"""

    channel = "local"
    name = "ollama"

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: float,
    ) -> ProviderReply:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in messages:
            if message["role"] == "assistant":
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.get("content") or "",
                }
                if message.get("tool_calls"):
                    entry["tool_calls"] = [
                        {
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                        }
                        for call in message["tool_calls"]
                    ]
                wire.append(entry)
            elif message["role"] == "tool":
                wire.append({"role": "tool", "content": message["content"]})
            else:
                wire.append({"role": "user", "content": message["content"]})
        decoded = _post_json(
            f"{self.base_url}/api/chat",
            {
                "model": self.model,
                "messages": wire,
                "stream": False,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("inputSchema")
                            or {"type": "object", "properties": {}},
                        },
                    }
                    for tool in tools
                ],
            },
            headers={},
            timeout=timeout,
        )
        payload = decoded.get("message")
        if not isinstance(payload, dict):
            raise ProviderError("Ollama 响应缺少 message")
        calls: list[ToolCall] = []
        for raw in payload.get("tool_calls") or []:
            function = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            if isinstance(name, str) and name:
                calls.append(
                    ToolCall(
                        call_id=new_id("call"),
                        name=name,
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
        content = payload.get("content")
        return ProviderReply(
            text=content if isinstance(content, str) else "",
            tool_calls=calls,
        )


class AnthropicProvider:
    """云端档：Anthropic Messages API（tool_use / tool_result 块）。

    仅在主人签发一次性授权票据后被调用；发出的内容以工具结果为限，
    工具面本身已按票据范围裁剪（见 loop 的构建方）。
    """

    channel = "cloud"
    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: float,
    ) -> ProviderReply:
        wire: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []
        for message in messages:
            if message["role"] == "tool":
                # Anthropic 要求同一轮的全部 tool_result 合并进一条 user 消息
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message["call_id"],
                        "content": message["content"],
                    }
                )
                continue
            if pending_results:
                wire.append({"role": "user", "content": pending_results})
                pending_results = []
            if message["role"] == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": message["content"]})
                for call in message.get("tool_calls") or []:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.call_id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": "user", "content": message["content"]})
        if pending_results:
            wire.append({"role": "user", "content": pending_results})
        decoded = _post_json(
            f"{self.base_url}/v1/messages",
            {
                "model": self.model,
                "max_tokens": 4096,
                "system": system,
                "messages": wire,
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    }
                    for tool in tools
                ],
            },
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=timeout,
        )
        if decoded.get("stop_reason") == "refusal":
            raise ProviderError("云端模型拒绝了本次请求")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in decoded.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        call_id=str(block.get("id") or new_id("call")),
                        name=str(block.get("name") or ""),
                        arguments=(
                            block["input"] if isinstance(block.get("input"), dict) else {}
                        ),
                    )
                )
        return ProviderReply(text="".join(text_parts), tool_calls=calls)


def build_local_provider(settings: Settings) -> OllamaProvider | None:
    if settings.assistant_provider != "ollama":
        return None
    return OllamaProvider(settings.ollama_url, settings.assistant_model)


def build_cloud_provider(settings: Settings) -> AnthropicProvider | None:
    if settings.assistant_cloud_provider != "anthropic":
        return None
    if not settings.assistant_cloud_api_key:
        return None
    return AnthropicProvider(
        settings.assistant_cloud_api_key,
        settings.assistant_cloud_model,
        settings.assistant_cloud_base_url,
    )

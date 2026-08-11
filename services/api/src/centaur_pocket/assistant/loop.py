"""Assistant orchestration loop (§3.4).

硬上限：6 轮工具调用、30 秒、单次响应 64 KiB。超限即停并如实说明——
不悄悄截断、不假装完成。provenance 由服务端在提议落库前强制注入，
模型自报的 provenance 一律丢弃：卡片页脚必须反映真实运行情况。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..mcp import MCPTool, ToolArgumentError
from .provider import ProviderReply, ToolCall

MAX_ROUNDS = 6
MAX_SECONDS = 30.0
MAX_RESPONSE_BYTES = 64 * 1024

SYSTEM_PROMPT = """你是主人的私人秘书 Agent，运行在主人自己的 Pocket 数据服务器上。

规则：
- 只依据工具返回的真实数据回答；查不到就说查不到，绝不编造。
- 只读工具可直接使用；propose_ 开头的工具只会把提议放进主人的待确认队列，
  永远不会直接写入业务数据。需要记录、安排或修改时，用对应的 propose_ 工具，
  并在 evidence 中给出依据（来源与摘录）。没有依据就不要提议。
- 你没有发邮件、下达任务、关闭任务、扩权等能力；不要声称你已完成这类操作。
- 回答用中文，简洁直接。提议提交后告诉主人到「事项 → 待确认」里确认。"""


def _tool_result_text(result: Any) -> str:
    try:
        text = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(result)
    # 单个工具结果也受响应上限约束，避免一次检索撑爆循环预算
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        text = text[: MAX_RESPONSE_BYTES // 4] + "…（结果过长已截断）"
    return text


class AssistantLoop:
    """Drives one owner turn: provider rounds + MCPTool execution."""

    def __init__(self, provider: Any, tools: list[MCPTool]) -> None:
        self.provider = provider
        self.tools = {tool.name: tool for tool in tools}
        self.definitions = [tool.definition for tool in tools]

    def _is_read_only(self, name: str) -> bool:
        tool = self.tools.get(name)
        if tool is None:
            return False
        annotations = tool.definition.get("annotations") or {}
        return bool(annotations.get("readOnlyHint"))

    def _provenance(
        self,
        *,
        rounds: int,
        retrieval_count: int,
        started: float,
        ticket_id: str | None,
    ) -> dict[str, Any]:
        provenance: dict[str, Any] = {
            "channel": self.provider.channel,
            "provider": self.provider.name,
            "model": self.provider.model,
            "tool_rounds": rounds,
            "retrieval_count": retrieval_count,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if ticket_id is not None:
            provenance["ticket_id"] = ticket_id
        return provenance

    def _execute_call(
        self,
        call: ToolCall,
        *,
        rounds: int,
        retrieval_count: int,
        started: float,
        ticket_id: str | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Returns (result text for the model, proposal dict if one was parked)."""

        tool = self.tools.get(call.name)
        if tool is None:
            return f"未知工具：{call.name}", None
        arguments = dict(call.arguments)
        if call.name.startswith("propose_"):
            # 服务端强制注入 provenance；模型无权自证出处
            arguments["provenance"] = self._provenance(
                rounds=rounds,
                retrieval_count=retrieval_count,
                started=started,
                ticket_id=ticket_id,
            )
        try:
            result = tool.handler(arguments)
        except ToolArgumentError as error:
            return f"参数错误：{error}", None
        except Exception as error:  # noqa: BLE001 - 工具失败要回给模型而非中断循环
            return f"{call.name} 执行失败：{str(error)[:300]}", None
        payload = dict(result) if isinstance(result, dict) else {"result": result}
        proposal = payload if "proposal_id" in payload else None
        return _tool_result_text(payload), proposal

    def run(self, message: str, *, ticket_id: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        conversation: list[dict[str, Any]] = [{"role": "user", "content": message}]
        proposals: list[dict[str, Any]] = []
        tool_calls_total = 0
        retrieval_count = 0
        rounds = 0
        stopped = "completed"
        reply_text = ""

        while rounds < MAX_ROUNDS:
            remaining = MAX_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                stopped = "timeout"
                break
            rounds += 1
            reply: ProviderReply = self.provider.chat(
                system=SYSTEM_PROMPT,
                messages=conversation,
                tools=self.definitions,
                timeout=remaining,
            )
            if len(reply.text.encode("utf-8")) > MAX_RESPONSE_BYTES:
                stopped = "response_too_large"
                break
            if not reply.tool_calls:
                reply_text = reply.text.strip()
                break
            conversation.append(
                {
                    "role": "assistant",
                    "content": reply.text,
                    "tool_calls": reply.tool_calls,
                }
            )
            for call in reply.tool_calls:
                tool_calls_total += 1
                result_text, proposal = self._execute_call(
                    call,
                    rounds=rounds,
                    retrieval_count=retrieval_count,
                    started=started,
                    ticket_id=ticket_id,
                )
                if proposal is not None:
                    proposals.append(proposal)
                if self._is_read_only(call.name):
                    try:
                        parsed = json.loads(result_text)
                        retrieval_count += int(parsed.get("count", 1)) if isinstance(
                            parsed, dict
                        ) else 1
                    except (ValueError, TypeError):
                        retrieval_count += 1
                conversation.append(
                    {
                        "role": "tool",
                        "call_id": call.call_id,
                        "name": call.name,
                        "content": result_text,
                    }
                )
        else:
            stopped = "round_limit"

        if stopped == "round_limit":
            reply_text = (
                f"已达到 {MAX_ROUNDS} 轮工具调用上限，我先停在这里。"
                "以上是目前查到的内容；如需继续，请再说一次或缩小范围。"
            )
        elif stopped == "timeout":
            reply_text = "本次处理超过 30 秒上限，我先停在这里。请缩小范围后重试。"
        elif stopped == "response_too_large":
            reply_text = "模型单次响应超过 64 KiB 上限，已停止。请缩小问题范围。"
        elif not reply_text:
            reply_text = "我没有得到有效回复。请换个说法再试一次。"

        provenance = self._provenance(
            rounds=rounds,
            retrieval_count=retrieval_count,
            started=started,
            ticket_id=ticket_id,
        )
        return {
            "reply": reply_text,
            "stopped": stopped,
            "provenance": provenance,
            "tool_calls": tool_calls_total,
            "proposals": proposals,
        }

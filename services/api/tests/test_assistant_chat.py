"""§3.4 模型编排循环：硬上限、provenance 强制注入、云端票据与审计。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from centaur_pocket.assistant.loop import MAX_ROUNDS, AssistantLoop
from centaur_pocket.assistant.provider import ProviderReply, ToolCall
from centaur_pocket.assistant.tickets import validate_scope
from centaur_pocket.main import create_app
from centaur_pocket.service import PocketError

OWNER_TOKEN = "cp_owner_test-token"


class FakeProvider:
    """脚本化 provider：按序返回预设回复，记录每轮收到的会话。"""

    channel = "local"
    name = "fake"
    model = "fake-model"

    def __init__(self, replies: list[ProviderReply]) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict[str, Any]]] = []

    def chat(self, *, system, messages, tools, timeout):  # noqa: ANN001
        assert timeout > 0
        self.seen.append([dict(message) for message in messages])
        if not self.replies:
            return ProviderReply(text="（没有更多脚本回复）")
        return self.replies.pop(0)


def _client_with_provider(tmp_path, provider) -> TestClient:
    from centaur_pocket.config import Settings

    settings = Settings(
        data_root=tmp_path / "assistant-runtime",
        owner_token=OWNER_TOKEN,
        agent_token="cp_live_assistant-test",
        scheduler_poll_seconds=0,
    )
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    app.state.assistant_local_provider = provider
    return client


@pytest.fixture
def owner_device_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {OWNER_TOKEN}",
        "X-Device-ID": "device-test-1",
    }


def test_loop_completes_and_forces_provenance(tmp_path, owner_device_headers):
    provider = FakeProvider(
        [
            ProviderReply(
                tool_calls=[
                    ToolCall(
                        call_id="call_1",
                        name="propose_memo",
                        arguments={
                            "title": "回访华东客户",
                            "content": "下周回访华东大客户，确认续约意向",
                            # 模型自报的 provenance 必须被服务端覆盖
                            "provenance": {"channel": "cloud", "ticket_id": "tkt_fake"},
                            "evidence": [
                                {"source": "owner", "excerpt": "主人口述要求回访"}
                            ],
                        },
                    )
                ]
            ),
            ProviderReply(text="已把回访备忘放进待确认队列。"),
        ]
    )
    client = _client_with_provider(tmp_path, provider)
    try:
        response = client.post(
            "/api/v1/assistant/chat",
            json={"message": "记一下：下周回访华东大客户"},
            headers=owner_device_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["stopped"] == "completed"
        assert body["reply"] == "已把回访备忘放进待确认队列。"
        assert body["tool_calls"] == 1
        assert len(body["proposals"]) == 1
        proposal = body["proposals"][0]
        # provenance 是服务端注入的真实运行情况，不是模型自报的
        assert proposal["provenance"]["channel"] == "local"
        assert proposal["provenance"]["provider"] == "fake"
        assert proposal["provenance"]["model"] == "fake-model"
        assert "ticket_id" not in proposal["provenance"]
        # 提议落进了 Owner 门控队列
        pending = client.get(
            "/api/v1/assistant/proposals",
            headers=owner_device_headers,
        ).json()
        assert pending["total"] == 1
        # 调用统计进入状态端点
        status_body = client.get(
            "/api/v1/assistant/status", headers=owner_device_headers
        ).json()
        assert status_body["calls_total"] == 1
        assert status_body["tool_calls_total"] == 1
        assert status_body["last_call"]["provider"] == "fake"
    finally:
        client.__exit__(None, None, None)


def test_loop_round_limit_stops_honestly(tmp_path, owner_device_headers):
    endless = ProviderReply(
        tool_calls=[ToolCall(call_id="c", name="today_brief", arguments={})]
    )
    provider = FakeProvider([endless] * (MAX_ROUNDS + 3))
    client = _client_with_provider(tmp_path, provider)
    try:
        response = client.post(
            "/api/v1/assistant/chat",
            json={"message": "今天怎么样"},
            headers=owner_device_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["stopped"] == "round_limit"
        assert body["provenance"]["tool_rounds"] == MAX_ROUNDS
        assert "上限" in body["reply"]
    finally:
        client.__exit__(None, None, None)


def test_chat_without_provider_returns_503(client, owner_headers):
    response = client.post(
        "/api/v1/assistant/chat",
        json={"message": "你好"},
        headers={**owner_headers, "X-Device-ID": "device-test-1"},
    )
    assert response.status_code == 503


def test_scope_rejects_contacts():
    with pytest.raises(PocketError) as error:
        validate_scope({"items": [{"category": "contacts"}]})
    assert error.value.status_code == 403


def test_cloud_ticket_is_single_use_and_audited(tmp_path, owner_device_headers):
    provider = FakeProvider([ProviderReply(text="云端回复")])
    provider.channel = "cloud"
    provider.name = "fake-cloud"
    client = _client_with_provider(tmp_path, provider)
    client.app.state.assistant_cloud_provider = provider
    try:
        issued = client.post(
            "/api/v1/assistant/cloud-tickets",
            json={
                "scope": {
                    "items": [
                        {"category": "tasks", "count": 5},
                        {"category": "schedule", "count": 3},
                    ]
                }
            },
            headers=owner_device_headers,
        )
        assert issued.status_code == 201
        ticket = issued.json()
        assert ticket["ttl_seconds"] == 300
        assert (
            client.get(
                "/api/v1/assistant/cloud-tickets", headers=owner_device_headers
            ).json()["total"]
            == 1
        )

        first = client.post(
            "/api/v1/assistant/chat",
            json={
                "message": "汇总本周任务",
                "channel": "cloud",
                "ticket_id": ticket["ticket_id"],
            },
            headers=owner_device_headers,
        )
        assert first.status_code == 200
        assert first.json()["provenance"]["ticket_id"] == ticket["ticket_id"]

        # 票据一次性：第二次使用同一票据被拒
        second = client.post(
            "/api/v1/assistant/chat",
            json={
                "message": "再来一次",
                "channel": "cloud",
                "ticket_id": ticket["ticket_id"],
            },
            headers=owner_device_headers,
        )
        assert second.status_code == 403

        # 审计事件落进工作区事件流
        audit = client.get(
            "/api/v1/workspaces/ws_default/audit",
            headers=owner_device_headers,
        ).json()
        cloud_calls = [
            change
            for change in audit["changes"]
            if change["event_type"] == "assistant.cloud_call"
        ]
        assert len(cloud_calls) == 1
        assert cloud_calls[0]["aggregate_type"] == "assistant"
        assert cloud_calls[0]["payload"]["scope"]["items"][0]["category"] == "tasks"
        assert cloud_calls[0]["device_id"] == "device-test-1"
    finally:
        client.__exit__(None, None, None)


def test_cloud_chat_requires_ticket(tmp_path, owner_device_headers):
    provider = FakeProvider([ProviderReply(text="不该到这里")])
    client = _client_with_provider(tmp_path, provider)
    client.app.state.assistant_cloud_provider = provider
    try:
        response = client.post(
            "/api/v1/assistant/chat",
            json={"message": "你好", "channel": "cloud"},
            headers=owner_device_headers,
        )
        assert response.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_knowledge_excluded_from_cloud_without_body_grant(tmp_path):
    """云端通道未勾选 documents 正文时，工具面里没有 knowledge_retrieve。"""

    captured: dict[str, Any] = {}

    class RecordingProvider(FakeProvider):
        channel = "cloud"
        name = "fake-cloud"

        def chat(self, *, system, messages, tools, timeout):  # noqa: ANN001
            captured["tools"] = [tool["name"] for tool in tools]
            return ProviderReply(text="好的")

    provider = RecordingProvider([])
    client = _client_with_provider(tmp_path, provider)
    client.app.state.assistant_cloud_provider = provider
    headers = {
        "Authorization": f"Bearer {OWNER_TOKEN}",
        "X-Device-ID": "device-test-1",
    }
    try:
        ticket = client.post(
            "/api/v1/assistant/cloud-tickets",
            json={"scope": {"items": [{"category": "tasks", "count": 5}]}},
            headers=headers,
        ).json()
        client.post(
            "/api/v1/assistant/chat",
            json={
                "message": "帮我看看",
                "channel": "cloud",
                "ticket_id": ticket["ticket_id"],
            },
            headers=headers,
        )
        assert "knowledge_retrieve" not in captured["tools"]
        assert "today_brief" in captured["tools"]

        # 勾选 documents 正文后，knowledge_retrieve 回到工具面
        ticket2 = client.post(
            "/api/v1/assistant/cloud-tickets",
            json={
                "scope": {
                    "items": [
                        {"category": "documents", "count": 5, "include_body": True}
                    ]
                }
            },
            headers=headers,
        ).json()
        client.post(
            "/api/v1/assistant/chat",
            json={
                "message": "帮我看看",
                "channel": "cloud",
                "ticket_id": ticket2["ticket_id"],
            },
            headers=headers,
        )
        assert "knowledge_retrieve" in captured["tools"]
    finally:
        client.__exit__(None, None, None)


def test_loop_timeout_reports_honestly(monkeypatch):
    """超时兜底：deadline 用尽后循环停止并如实说明。"""

    class SlowProvider:
        channel = "local"
        name = "slow"
        model = "slow-model"

        def chat(self, *, system, messages, tools, timeout):  # noqa: ANN001
            import time as time_module

            # 模拟耗尽全部时间预算（不真的 sleep 30 秒）
            monkeypatch.setattr(
                "centaur_pocket.assistant.loop.time",
                type(
                    "T",
                    (),
                    {"monotonic": staticmethod(lambda: time_module.monotonic() + 31)},
                ),
            )
            return ProviderReply(
                tool_calls=[ToolCall(call_id="c", name="today_brief", arguments={})]
            )

    loop = AssistantLoop(SlowProvider(), [])
    result = loop.run("测试")
    assert result["stopped"] == "timeout"
    assert "30 秒" in result["reply"]

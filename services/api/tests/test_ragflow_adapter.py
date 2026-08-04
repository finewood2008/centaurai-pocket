from __future__ import annotations

import json
from urllib.request import Request

import pytest

from centaur_pocket.ragflow_adapter import (
    RAGFlowAdapter,
    RAGFlowConfig,
    RAGFlowError,
)


class FakeRAGFlow:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.dataset_exists = False

    def __call__(self, request: Request, _timeout: float) -> dict:
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.method, request.full_url, body))
        if request.method == "GET" and "/datasets?" in request.full_url:
            data = [{"id": "ds_work", "name": "Pocket Work"}] if self.dataset_exists else []
            return {"code": 0, "data": data}
        if request.method == "POST" and request.full_url.endswith("/datasets"):
            self.dataset_exists = True
            return {"code": 0, "data": {"id": "ds_work"}}
        if "documents?type=empty" in request.full_url:
            return {"code": 0, "data": [{"id": "doc_1"}]}
        if request.full_url.endswith("/chunks"):
            return {"code": 0, "data": {"chunk": {"id": "chunk_1"}}}
        if request.full_url.endswith("/retrieval"):
            return {"code": 0, "data": {"chunks": [{"id": "chunk_1"}]}}
        raise AssertionError(request.full_url)


def config() -> RAGFlowConfig:
    return RAGFlowConfig(
        base_url="http://127.0.0.1:9380",
        api_key="rag-secret",
        dataset_name="Pocket Work",
    )


def test_projection_round_trip_uses_public_ragflow_api() -> None:
    fake = FakeRAGFlow()
    adapter = RAGFlowAdapter(config(), opener=fake)

    dataset_id = adapter.ensure_dataset()
    document_id = adapter.create_projection_document(dataset_id, "conversation-1.txt")
    chunk_id = adapter.add_chunk(
        dataset_id=dataset_id,
        document_id=document_id,
        content="张三明确同意下周一发布。",
        keywords=["张三", "发布"],
    )
    results = adapter.retrieve(dataset_id=dataset_id, query="什么时候发布？")

    assert (dataset_id, document_id, chunk_id) == ("ds_work", "doc_1", "chunk_1")
    assert results == [{"id": "chunk_1"}]
    assert all("rag-secret" not in url for _, url, _ in fake.calls)
    retrieval_body = fake.calls[-1][2]
    assert retrieval_body["dataset_ids"] == ["ds_work"]
    assert retrieval_body["keyword"] is True


def test_existing_dataset_is_reused_case_insensitively() -> None:
    fake = FakeRAGFlow()
    fake.dataset_exists = True
    adapter = RAGFlowAdapter(config(), opener=fake)

    assert adapter.ensure_dataset() == "ds_work"
    assert [method for method, _, _ in fake.calls] == ["GET"]


def test_invalid_or_failed_responses_fail_closed() -> None:
    with pytest.raises(ValueError):
        RAGFlowConfig(base_url="file:///tmp/ragflow", api_key="x", dataset_name="y")
    with pytest.raises(ValueError):
        RAGFlowConfig(
            base_url="https://user@ragflow.test",
            api_key="x",
            dataset_name="y",
        )
    with pytest.raises(ValueError):
        RAGFlowConfig(
            base_url="https://ragflow.test?redirect=elsewhere",
            api_key="x",
            dataset_name="y",
        )

    adapter = RAGFlowAdapter(
        config(),
        opener=lambda _request, _timeout: {"code": 101, "message": "denied"},
    )
    with pytest.raises(RAGFlowError, match="denied"):
        adapter.ensure_dataset()

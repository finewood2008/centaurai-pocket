from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class RAGFlowError(RuntimeError):
    """Raised when the optional RAGFlow engine rejects a request."""


JsonOpener = Callable[[urllib.request.Request, float], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RAGFlowConfig:
    base_url: str
    api_key: str
    dataset_name: str
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RAGFlow base_url 必须是 HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("RAGFlow base_url 不能包含凭据、查询参数或片段")
        if not self.api_key.strip():
            raise ValueError("RAGFlow api_key 不能为空")
        if not self.dataset_name.strip():
            raise ValueError("RAGFlow dataset_name 不能为空")


class RAGFlowAdapter:
    """Small, optional boundary around RAGFlow's public HTTP API.

    Pocket remains the authority for identity, message history, governance and
    evidence links.  This adapter only mirrors governed text into a private
    RAGFlow dataset and asks RAGFlow for hybrid retrieval candidates.
    """

    def __init__(
        self,
        config: RAGFlowConfig,
        *,
        opener: JsonOpener | None = None,
    ) -> None:
        self.config = config
        self._opener = opener or self._open_json

    def ensure_dataset(self) -> str:
        query = urllib.parse.urlencode(
            {"name": self.config.dataset_name, "page": 1, "page_size": 10}
        )
        listed = self._request("GET", f"/api/v1/datasets?{query}")
        datasets = self._data_list(listed)
        for dataset in datasets:
            if str(dataset.get("name", "")).casefold() == self.config.dataset_name.casefold():
                dataset_id = str(dataset.get("id", "")).strip()
                if dataset_id:
                    return dataset_id

        created = self._request(
            "POST",
            "/api/v1/datasets",
            {
                "name": self.config.dataset_name,
                "description": "CentaurAI Pocket governed IM knowledge projection",
                "permission": "me",
                "chunk_method": "manual",
            },
        )
        data = created.get("data")
        if isinstance(data, dict) and str(data.get("id", "")).strip():
            return str(data["id"])
        raise RAGFlowError("RAGFlow 创建 dataset 后没有返回 id")

    def create_projection_document(self, dataset_id: str, name: str) -> str:
        escaped = urllib.parse.quote(dataset_id, safe="")
        response = self._request(
            "POST",
            f"/api/v1/datasets/{escaped}/documents?type=empty",
            {"name": name},
        )
        documents = self._data_list(response)
        if documents and str(documents[0].get("id", "")).strip():
            return str(documents[0]["id"])
        raise RAGFlowError("RAGFlow 创建投影文档后没有返回 id")

    def add_chunk(
        self,
        *,
        dataset_id: str,
        document_id: str,
        content: str,
        keywords: list[str] | None = None,
        questions: list[str] | None = None,
    ) -> str:
        if not content.strip():
            raise ValueError("RAGFlow chunk content 不能为空")
        dataset = urllib.parse.quote(dataset_id, safe="")
        document = urllib.parse.quote(document_id, safe="")
        response = self._request(
            "POST",
            f"/api/v1/datasets/{dataset}/documents/{document}/chunks",
            {
                "content": content,
                "important_keywords": keywords or [],
                "questions": questions or [],
                "tag_kwd": ["CentaurAI", "IM", "governed"],
            },
        )
        data = response.get("data")
        if isinstance(data, dict):
            chunk = data.get("chunk")
            if isinstance(chunk, dict) and str(chunk.get("id", "")).strip():
                return str(chunk["id"])
        raise RAGFlowError("RAGFlow 创建 chunk 后没有返回 id")

    def retrieve(
        self,
        *,
        dataset_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        response = self._request(
            "POST",
            "/api/v1/retrieval",
            {
                "question": query,
                "dataset_ids": [dataset_id],
                "page": 1,
                "page_size": max(1, min(limit, 50)),
                "similarity_threshold": 0.2,
                "vector_similarity_weight": 0.3,
                "keyword": True,
                "highlight": False,
            },
        )
        return self._data_list(response, key="chunks")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        response = self._opener(request, self.config.timeout_seconds)
        code = response.get("code", 0)
        if code not in (0, None):
            message = str(response.get("message") or "RAGFlow 请求失败")
            raise RAGFlowError(message)
        return response

    @staticmethod
    def _open_json(
        request: urllib.request.Request,
        timeout: float,
    ) -> dict[str, Any]:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                detail = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = raw.decode("utf-8", errors="replace")[:500]
            raise RAGFlowError(f"RAGFlow HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RAGFlowError(f"无法连接 RAGFlow：{error}") from error
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RAGFlowError("RAGFlow 返回了无效 JSON") from error
        if not isinstance(parsed, dict):
            raise RAGFlowError("RAGFlow 返回格式不是对象")
        return parsed

    @staticmethod
    def _data_list(
        response: dict[str, Any],
        *,
        key: str | None = None,
    ) -> list[dict[str, Any]]:
        data: Any = response.get("data")
        if key and isinstance(data, dict):
            data = data.get(key)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward a credential-bearing RAGFlow request through redirects."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl

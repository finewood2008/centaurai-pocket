from __future__ import annotations

import ctypes
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self


class WeComArchiveError(RuntimeError):
    """Raised when the official Enterprise WeChat archive SDK fails."""


class WeComArchiveSDK(Protocol):
    def get_chat_data(self, *, seq: int, limit: int) -> dict[str, Any]: ...

    def decrypt_data(
        self,
        *,
        decrypted_random_key: str,
        encrypted_message: str,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class WeComRandomKeyDecryptor(Protocol):
    """Decrypt the SDK envelope key with the configured versioned RSA key."""

    def __call__(
        self, *, encrypted_random_key: str, public_key_version: int
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class WeComArchivePage:
    events: tuple[dict[str, Any], ...]
    next_seq: int
    has_more: bool


class WeComArchiveCollector:
    """Transforms official SDK pages into Pocket's provider-neutral events.

    Cursor persistence and raw-event durability belong to Pocket's ingestion
    service.  The caller must only commit ``next_seq`` after every returned raw
    event has been stored successfully.
    """

    def __init__(
        self,
        sdk: WeComArchiveSDK,
        *,
        archived_member_ids: set[str],
        decrypt_random_key: WeComRandomKeyDecryptor,
    ) -> None:
        self.sdk = sdk
        self.archived_member_ids = archived_member_ids
        self.decrypt_random_key = decrypt_random_key

    def pull_page(self, *, seq: int, limit: int = 1000) -> WeComArchivePage:
        if seq < 0:
            raise ValueError("企业微信 seq 不能为负数")
        if not 1 <= limit <= 1000:
            raise ValueError("企业微信单页 limit 必须在 1 到 1000 之间")
        envelope = self.sdk.get_chat_data(seq=seq, limit=limit)
        if int(envelope.get("errcode", 0)) != 0:
            raise WeComArchiveError(
                str(envelope.get("errmsg") or "企业微信 GetChatData 失败")
            )
        chatdata = envelope.get("chatdata")
        if not isinstance(chatdata, list):
            raise WeComArchiveError("企业微信 GetChatData 返回缺少 chatdata")

        events: list[dict[str, Any]] = []
        max_seq = seq
        for encrypted in chatdata:
            if not isinstance(encrypted, dict):
                raise WeComArchiveError("企业微信 chatdata 包含无效记录")
            event_seq = _required_int(encrypted, "seq")
            public_key_version = _required_int(encrypted, "publickey_ver")
            encrypted_key = _required_text(encrypted, "encrypt_random_key")
            encrypted_message = _required_text(encrypted, "encrypt_chat_msg")
            decrypted_key = self.decrypt_random_key(
                encrypted_random_key=encrypted_key,
                public_key_version=public_key_version,
            )
            if not isinstance(decrypted_key, str) or not decrypted_key:
                raise WeComArchiveError("企业微信随机密钥解密结果为空")
            decrypted = self.sdk.decrypt_data(
                decrypted_random_key=decrypted_key,
                encrypted_message=encrypted_message,
            )
            events.append(
                normalize_wecom_message(
                    decrypted,
                    source_seq=event_seq,
                    archived_member_ids=self.archived_member_ids,
                )
            )
            max_seq = max(max_seq, event_seq)

        return WeComArchivePage(
            events=tuple(events),
            next_seq=max_seq,
            has_more=len(chatdata) == limit,
        )


def normalize_wecom_message(
    message: dict[str, Any],
    *,
    source_seq: int,
    archived_member_ids: set[str],
) -> dict[str, Any]:
    provider_msgid = _required_text(message, "msgid")
    sender = _required_text(message, "from")
    recipients = message.get("tolist")
    if not isinstance(recipients, list):
        recipients = []
    recipient_ids = sorted(
        str(value).strip() for value in recipients if str(value).strip()
    )
    room_id = str(message.get("roomid") or "").strip()
    if room_id:
        conversation_id = room_id
        conversation_type = "group"
    else:
        participants = sorted({sender, *recipient_ids})
        conversation_id = "direct:" + ":".join(participants)
        conversation_type = "direct"

    msg_type = str(message.get("msgtype") or "unknown").strip() or "unknown"
    normalized_message_type = (
        msg_type
        if msg_type in {"text", "image", "voice", "file", "video", "system"}
        else "other"
    )
    msg_time = _required_int(message, "msgtime")
    try:
        sent_at = datetime.fromtimestamp(msg_time / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError) as error:
        raise WeComArchiveError("企业微信消息时间超出支持范围") from error

    text = None
    typed_payload = message.get(msg_type)
    if msg_type == "text" and isinstance(typed_payload, dict):
        candidate = typed_payload.get("content")
        if isinstance(candidate, str):
            text = candidate

    media_references = sorted(_collect_sdk_file_ids(message))
    return {
        "provider_msgid": provider_msgid,
        "provider_conversation_id": conversation_id,
        "conversation_name": None,
        "conversation_type": conversation_type,
        "direction": "outgoing" if sender in archived_member_ids else "incoming",
        "message_type": normalized_message_type,
        "provider_message_type": msg_type,
        "sender_provider_id": sender,
        "sender_display_name": None,
        "text": text,
        "displayed_time_text": None,
        "sent_at": sent_at,
        "observed_at": datetime.now(UTC).isoformat(),
        "source_seq": source_seq,
        "action": str(message.get("action") or "send"),
        "media_references": media_references,
        "raw": message,
    }


def _collect_sdk_file_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "sdkfileid" and isinstance(child, str) and child.strip():
                found.add(child.strip())
            else:
                found.update(_collect_sdk_file_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_sdk_file_ids(child))
    return found


def _required_text(value: dict[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise WeComArchiveError(f"企业微信消息缺少 {key}")
    return candidate.strip()


def _required_int(value: dict[str, Any], key: str) -> int:
    candidate = value.get(key)
    if isinstance(candidate, bool):
        raise WeComArchiveError(f"企业微信消息 {key} 格式错误")
    try:
        return int(candidate)
    except (TypeError, ValueError) as error:
        raise WeComArchiveError(f"企业微信消息缺少有效 {key}") from error


class NativeWeComArchiveSDK:
    """Minimal ctypes wrapper for the official WeWork Finance C SDK.

    The proprietary SDK is deliberately not vendored.  Deployments provide the
    official shared library and corresponding OpenSSL runtime, while tests use
    a fixture implementation of :class:`WeComArchiveSDK`.
    """

    def __init__(
        self,
        *,
        library_path: Path,
        corp_id: str,
        secret: str,
        proxy: str = "",
        proxy_password: str = "",
        timeout_seconds: int = 10,
    ) -> None:
        if not library_path.is_file():
            raise WeComArchiveError(f"企业微信 SDK 不存在：{library_path}")
        self._library = ctypes.CDLL(str(library_path))
        self._configure_signatures()
        self._sdk = self._library.NewSdk()
        if not self._sdk:
            raise WeComArchiveError("企业微信 NewSdk 失败")
        self._proxy = proxy.encode("utf-8")
        self._proxy_password = proxy_password.encode("utf-8")
        self._timeout_seconds = timeout_seconds
        result = self._library.Init(
            self._sdk,
            corp_id.encode("utf-8"),
            secret.encode("utf-8"),
        )
        if result != 0:
            self.close()
            raise WeComArchiveError(f"企业微信 SDK Init 失败：{result}")

    def _configure_signatures(self) -> None:
        library = self._library
        library.NewSdk.restype = ctypes.c_void_p
        library.DestroySdk.argtypes = [ctypes.c_void_p]
        library.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        library.Init.restype = ctypes.c_int
        library.NewSlice.restype = ctypes.c_void_p
        library.FreeSlice.argtypes = [ctypes.c_void_p]
        library.GetContentFromSlice.argtypes = [ctypes.c_void_p]
        library.GetContentFromSlice.restype = ctypes.c_char_p
        library.GetChatData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulonglong,
            ctypes.c_uint,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        library.GetChatData.restype = ctypes.c_int
        library.DecryptData.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
        ]
        library.DecryptData.restype = ctypes.c_int

    def get_chat_data(self, *, seq: int, limit: int) -> dict[str, Any]:
        output = self._library.NewSlice()
        try:
            result = self._library.GetChatData(
                self._sdk,
                seq,
                limit,
                self._proxy,
                self._proxy_password,
                self._timeout_seconds,
                output,
            )
            if result != 0:
                raise WeComArchiveError(f"企业微信 GetChatData SDK 错误：{result}")
            return self._slice_json(output)
        finally:
            self._library.FreeSlice(output)

    def decrypt_data(
        self,
        *,
        decrypted_random_key: str,
        encrypted_message: str,
    ) -> dict[str, Any]:
        output = self._library.NewSlice()
        try:
            result = self._library.DecryptData(
                decrypted_random_key.encode("utf-8"),
                encrypted_message.encode("utf-8"),
                output,
            )
            if result != 0:
                raise WeComArchiveError(f"企业微信 DecryptData SDK 错误：{result}")
            return self._slice_json(output)
        finally:
            self._library.FreeSlice(output)

    def _slice_json(self, output: int) -> dict[str, Any]:
        raw = self._library.GetContentFromSlice(output)
        if not raw:
            raise WeComArchiveError("企业微信 SDK 返回空内容")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WeComArchiveError("企业微信 SDK 返回无效 JSON") from error
        if not isinstance(parsed, dict):
            raise WeComArchiveError("企业微信 SDK JSON 不是对象")
        return parsed

    def close(self) -> None:
        sdk = getattr(self, "_sdk", None)
        if sdk:
            self._library.DestroySdk(sdk)
            self._sdk = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

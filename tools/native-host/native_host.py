#!/usr/bin/env python3
"""Firefox Native Messaging bridge for the CentaurAI WeChat observer.

The process accepts only a small, versioned message schema from the fixed
extension ID and forwards it to a loopback-only Pocket collector API. Secrets
are loaded from a user-owned 0600 JSON file and never returned to the browser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Mapping


APP_NAME = "ai.centaur.pocket.wechat_observer"
EXTENSION_ID = "centaur-pocket-wechat-observer@centaur.ai"
DEFAULT_API_BASE = "http://127.0.0.1:8718"
MAX_NATIVE_MESSAGE_BYTES = 256 * 1024
MAX_API_RESPONSE_BYTES = 256 * 1024
MAX_EVENTS_PER_BATCH = 50
MAX_TEXT_LENGTH = 16_000
MAX_ID_LENGTH = 500
MAX_TOKEN_LENGTH = 4_096
REQUEST_TIMEOUT_SECONDS = 8.0
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MESSAGE_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class HostError(Exception):
    """A safe error that may be returned to the extension."""


class ProtocolError(HostError):
    """The native message or collector payload did not match the schema."""


class ConfigError(HostError):
    """The local observer configuration is absent or unsafe."""


def default_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "centaurai-pocket" / "wechat-observer.json"


def validate_api_base(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 256:
        raise ConfigError("api_base 必须是本机 HTTP 地址")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ConfigError("api_base 只允许 http://127.0.0.1:<port>")
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigError("api_base 端口无效") from error
    if port is None or not 1 <= port <= 65535:
        raise ConfigError("api_base 必须包含有效端口")
    return f"http://127.0.0.1:{port}"


def validate_source_id(value: Any) -> str:
    if not isinstance(value, str) or not SOURCE_ID_PATTERN.fullmatch(value):
        raise ConfigError("source_id 格式无效")
    return value


def validate_secret(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN_LENGTH:
        raise ConfigError(f"{name} 缺失或长度无效")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ConfigError(f"{name} 必须使用不含空格的可打印 ASCII 字符")
    return value


def ensure_secure_regular_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ConfigError("观察器尚未配置，请先运行安装脚本") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError("观察器配置必须是普通文件，不能是符号链接")
    if info.st_uid != os.getuid():
        raise ConfigError("观察器配置不属于当前用户")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError("观察器配置权限必须为 0600")


def read_json_object(path: Path, maximum_bytes: int = 64 * 1024) -> dict[str, Any]:
    ensure_secure_regular_file(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigError("无法读取观察器配置") from error
    if len(raw) > maximum_bytes:
        raise ConfigError("观察器配置过大")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("观察器配置不是有效 JSON") from error
    if not isinstance(value, dict):
        raise ConfigError("观察器配置必须是 JSON 对象")
    return value


@dataclass(frozen=True)
class HostConfig:
    api_base: str
    source_id: str
    collector_token: str | None
    pairing_code: str | None
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "HostConfig":
        resolved = (path or default_config_path()).expanduser().absolute()
        value = read_json_object(resolved)
        allowed = {"schema_version", "api_base", "source_id", "collector_token", "pairing_code"}
        if set(value) - allowed:
            raise ConfigError("观察器配置包含未知字段")
        if value.get("schema_version") != 1:
            raise ConfigError("观察器配置版本不受支持")
        collector = value.get("collector_token")
        pairing = value.get("pairing_code")
        if collector is not None:
            collector = validate_secret(collector, "collector_token")
        if pairing is not None:
            pairing = validate_secret(pairing, "pairing_code")
        if bool(collector) == bool(pairing):
            if collector:
                raise ConfigError("collector_token 与 pairing_code 不能同时存在")
            raise ConfigError("配置中没有 collector_token 或 pairing_code")
        return cls(
            api_base=validate_api_base(value.get("api_base")),
            source_id=validate_source_id(value.get("source_id")),
            collector_token=collector,
            pairing_code=pairing,
            path=resolved,
        )

    def persist_collector_token(self, token: str) -> "HostConfig":
        validated = validate_secret(token, "collector_token")
        write_config_file(
            self.path,
            {
                "schema_version": 1,
                "api_base": self.api_base,
                "source_id": self.source_id,
                "collector_token": validated,
            },
        )
        return HostConfig(
            api_base=self.api_base,
            source_id=self.source_id,
            collector_token=validated,
            pairing_code=None,
            path=self.path,
        )


def write_config_file(path: Path, value: Mapping[str, Any]) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ConfigError("观察器配置必须是普通文件，不能是符号链接")
        if info.st_uid != os.getuid():
            raise ConfigError("观察器配置不属于当前用户")
    try:
        os.chmod(path.parent, 0o700)
    except OSError as error:
        raise ConfigError("无法保护观察器配置目录") from error
    encoded = (json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".wechat-observer-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise ConfigError("无法安全写入观察器配置") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} 必须是对象")
    return value


def reject_unknown_keys(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ProtocolError(f"{name} 包含未知字段")


def require_string(
    value: Mapping[str, Any],
    key: str,
    *,
    maximum: int,
    optional: bool = False,
    pattern: re.Pattern[str] | None = None,
    allow_multiline: bool = False,
) -> str | None:
    item = value.get(key)
    if item is None and optional:
        return None
    if not isinstance(item, str) or not item or len(item) > maximum:
        raise ProtocolError(f"{key} 缺失或长度无效")
    if any(
        (ord(character) < 0x20 and not (allow_multiline and character in "\n\t"))
        or ord(character) == 0x7F
        for character in item
    ):
        raise ProtocolError(f"{key} 包含控制字符")
    if pattern and not pattern.fullmatch(item):
        raise ProtocolError(f"{key} 格式无效")
    return item


def require_integer(
    value: Mapping[str, Any], key: str, *, minimum: int, maximum: int, optional: bool = False
) -> int | None:
    item = value.get(key)
    if item is None and optional:
        return None
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise ProtocolError(f"{key} 必须是 {minimum} 到 {maximum} 的整数")
    return item


def require_timestamp(value: Mapping[str, Any], key: str, *, optional: bool = False) -> str | None:
    item = require_string(value, key, maximum=64, optional=optional)
    if item is None:
        return None
    candidate = item[:-1] + "+00:00" if item.endswith("Z") else item
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ProtocolError(f"{key} 不是有效 ISO 8601 时间") from error
    if parsed.tzinfo is None:
        raise ProtocolError(f"{key} 必须包含时区")
    return item


def validate_handshake(body: Any) -> dict[str, Any]:
    value = require_object(body, "handshake.body")
    allowed = {
        "extension_id",
        "extension_version",
        "browser_name",
        "browser_version",
        "parser_version",
    }
    reject_unknown_keys(value, allowed, "handshake.body")
    extension_id = require_string(value, "extension_id", maximum=128)
    if extension_id != EXTENSION_ID:
        raise ProtocolError("扩展 ID 不匹配")
    browser_name = require_string(value, "browser_name", maximum=64)
    if browser_name.casefold() != "firefox":
        raise ProtocolError("browser_name 只允许 firefox")
    result = {
        "extension_id": extension_id,
        "extension_version": require_string(value, "extension_version", maximum=64),
        "browser_name": "firefox",
        "parser_version": require_string(value, "parser_version", maximum=64),
    }
    browser_version = require_string(value, "browser_version", maximum=64, optional=True)
    if browser_version is not None:
        result["browser_version"] = browser_version
    return result


def validate_heartbeat(body: Any) -> dict[str, Any]:
    value = require_object(body, "heartbeat.body")
    allowed = {
        "browser_session_id",
        "state",
        "observed_at",
        "browser_version",
        "extension_version",
        "parser_version",
        "current_conversation_id",
        "current_conversation_name",
        "unread_conversation_count",
    }
    reject_unknown_keys(value, allowed, "heartbeat.body")
    states = {
        "login_required",
        "awaiting_phone_confirm",
        "active",
        "capture_paused",
        "browser_offline",
        "parser_degraded",
        "account_rejected",
    }
    result: dict[str, Any] = {
        "browser_session_id": require_string(value, "browser_session_id", maximum=128),
        "state": require_string(value, "state", maximum=64),
        "extension_version": require_string(value, "extension_version", maximum=64),
        "parser_version": require_string(value, "parser_version", maximum=64),
    }
    if result["state"] not in states:
        raise ProtocolError("state 不受支持")
    for key in ("browser_version", "current_conversation_id", "current_conversation_name"):
        item = require_string(value, key, maximum=MAX_ID_LENGTH, optional=True)
        if item is not None:
            result[key] = item
    observed = require_timestamp(value, "observed_at", optional=True)
    if observed is not None:
        result["observed_at"] = observed
    unread = require_integer(value, "unread_conversation_count", minimum=0, maximum=100_000, optional=True)
    if unread is not None:
        result["unread_conversation_count"] = unread
    return result


def validate_event(body: Any) -> dict[str, Any]:
    value = require_object(body, "event")
    allowed = {
        "provider_msgid",
        "provider_conversation_id",
        "conversation_name",
        "conversation_type",
        "direction",
        "message_type",
        "sender_provider_id",
        "sender_display_name",
        "text",
        "displayed_time_text",
        "sent_at",
        "observed_at",
    }
    reject_unknown_keys(value, allowed, "event")
    result: dict[str, Any] = {
        "provider_msgid": require_string(value, "provider_msgid", maximum=MAX_ID_LENGTH),
        "provider_conversation_id": require_string(
            value, "provider_conversation_id", maximum=MAX_ID_LENGTH
        ),
        "conversation_type": require_string(value, "conversation_type", maximum=32),
        "direction": require_string(value, "direction", maximum=32),
        "message_type": require_string(
            value, "message_type", maximum=64, pattern=MESSAGE_TYPE_PATTERN
        ),
        "observed_at": require_timestamp(value, "observed_at"),
    }
    if result["conversation_type"] not in {"direct", "group", "unknown"}:
        raise ProtocolError("conversation_type 不受支持")
    if result["direction"] not in {"incoming", "outgoing", "system", "unknown"}:
        raise ProtocolError("direction 不受支持")
    if result["message_type"] not in {
        "text",
        "image",
        "voice",
        "file",
        "video",
        "system",
        "other",
    }:
        raise ProtocolError("message_type 不受支持")
    for key, maximum in (
        ("conversation_name", MAX_ID_LENGTH),
        ("sender_provider_id", MAX_ID_LENGTH),
        ("sender_display_name", MAX_ID_LENGTH),
        ("text", MAX_TEXT_LENGTH),
        ("displayed_time_text", 128),
    ):
        item = require_string(
            value,
            key,
            maximum=maximum,
            optional=True,
            allow_multiline=key == "text",
        )
        if item is not None:
            result[key] = item
    sent_at = require_timestamp(value, "sent_at", optional=True)
    if sent_at is not None:
        result["sent_at"] = sent_at
    return result


def validate_events(body: Any) -> tuple[dict[str, Any], int]:
    value = require_object(body, "events.body")
    reject_unknown_keys(value, {"batch_id", "browser_session_id", "events"}, "events.body")
    events = value.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS_PER_BATCH:
        raise ProtocolError(f"events 必须包含 1 到 {MAX_EVENTS_PER_BATCH} 条消息")
    result = {
        "batch_id": require_string(value, "batch_id", maximum=128),
        "browser_session_id": require_string(value, "browser_session_id", maximum=128),
        "events": [validate_event(event) for event in events],
    }
    return result, len(events)


def validate_configure(body: Any) -> dict[str, Any]:
    value = require_object(body, "configure.body")
    reject_unknown_keys(
        value,
        {"extension_id", "api_base", "source_id", "pairing_code"},
        "configure.body",
    )
    extension_id = require_string(value, "extension_id", maximum=128)
    if extension_id != EXTENSION_ID:
        raise ProtocolError("扩展 ID 不匹配")
    try:
        api_base = validate_api_base(value.get("api_base"))
        source_id = validate_source_id(value.get("source_id"))
        pairing_code = validate_secret(value.get("pairing_code"), "pairing_code")
    except ConfigError as error:
        raise ProtocolError(str(error)) from error
    return {
        "extension_id": extension_id,
        "api_base": api_base,
        "source_id": source_id,
        "pairing_code": pairing_code,
    }


def validate_envelope(message: Any) -> tuple[str, str, dict[str, Any], int]:
    value = require_object(message, "native message")
    reject_unknown_keys(value, {"type", "request_id", "body"}, "native message")
    message_type = require_string(value, "type", maximum=32)
    request_id = require_string(value, "request_id", maximum=128)
    if message_type == "configure":
        return message_type, request_id, validate_configure(value.get("body")), 0
    if message_type == "handshake":
        return message_type, request_id, validate_handshake(value.get("body")), 0
    if message_type == "heartbeat":
        return message_type, request_id, validate_heartbeat(value.get("body")), 0
    if message_type == "events":
        body, event_count = validate_events(value.get("body"))
        return message_type, request_id, body, event_count
    raise ProtocolError("不支持的 native message 类型")


class SlidingWindowLimiter:
    def __init__(self, max_messages: int = 120, max_events: int = 1_000, window_seconds: float = 60.0):
        self.max_messages = max_messages
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.messages: deque[float] = deque()
        self.events: deque[tuple[float, int]] = deque()
        self.event_total = 0

    def check(self, event_count: int, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        while self.messages and self.messages[0] <= cutoff:
            self.messages.popleft()
        while self.events and self.events[0][0] <= cutoff:
            _, count = self.events.popleft()
            self.event_total -= count
        if len(self.messages) >= self.max_messages or self.event_total + event_count > self.max_events:
            raise ProtocolError("观察器提交过于频繁，请稍后重试")
        self.messages.append(current)
        if event_count:
            self.events.append((current, event_count))
            self.event_total += event_count


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class CollectorClient:
    def __init__(self, config: HostConfig):
        self.config = config
        # Never inherit HTTP(S)_PROXY for a loopback credential-bearing request.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirectHandler(),
        )

    def _post(self, endpoint: str, body: Mapping[str, Any], token: str) -> dict[str, Any]:
        encoded = json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_NATIVE_MESSAGE_BYTES:
            raise ProtocolError("提交批次过大")
        url = f"{self.config.api_base}/api/v1/collectors/v1/sources/{self.config.source_id}/{endpoint}"
        request = urllib.request.Request(
            url,
            data=encoded,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "CentaurAI-WeChat-Native-Host/0.1.0",
            },
        )
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(raw) > MAX_API_RESPONSE_BYTES:
                    raise HostError("Pocket API 响应过大")
        except urllib.error.HTTPError as error:
            raise HostError(f"Pocket API 返回 HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise HostError("无法连接本机 Pocket API") from error
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HostError("Pocket API 返回无效 JSON") from error
        if not isinstance(parsed, dict):
            raise HostError("Pocket API 响应格式无效")
        return parsed

    def handshake(self, body: Mapping[str, Any]) -> None:
        if self.config.collector_token:
            return
        if not self.config.pairing_code:
            raise ConfigError("没有可用的配对码")
        response = self._post("handshake", body, self.config.pairing_code)
        token = response.get("collector_token")
        if not isinstance(token, str):
            raise HostError("Pocket API 未返回 collector_token")
        self.config = self.config.persist_collector_token(token)

    def post_authenticated(self, endpoint: str, body: Mapping[str, Any]) -> None:
        if not self.config.collector_token:
            raise ConfigError("观察器尚未完成配对")
        self._post(endpoint, body, self.config.collector_token)


class NativeHost:
    def __init__(self, config_path: Path | None = None):
        self.config_path = (config_path or default_config_path()).expanduser().absolute()
        self.client: CollectorClient | None = None
        self.config_error: str | None = None
        try:
            self.client = CollectorClient(HostConfig.load(self.config_path))
        except ConfigError as error:
            self.config_error = str(error)
        self.limiter = SlidingWindowLimiter()

    def handle(self, message: Any) -> dict[str, Any]:
        request_id = message.get("request_id") if isinstance(message, dict) else None
        if not isinstance(request_id, str) or len(request_id) > 128:
            request_id = "invalid"
        try:
            message_type, request_id, body, event_count = validate_envelope(message)
            self.limiter.check(event_count)
            if message_type == "configure":
                write_config_file(
                    self.config_path,
                    {
                        "schema_version": 1,
                        "api_base": body["api_base"],
                        "source_id": body["source_id"],
                        "pairing_code": body["pairing_code"],
                    },
                )
                self.client = CollectorClient(HostConfig.load(self.config_path))
                self.config_error = None
            elif self.client is None:
                raise ConfigError(self.config_error or "观察器尚未配置")
            elif message_type == "handshake":
                self.client.handshake(body)
            else:
                self.client.post_authenticated(message_type, body)
            return {"request_id": request_id, "ok": True}
        except HostError as error:
            return {"request_id": request_id, "ok": False, "error": str(error)}
        except Exception:
            return {"request_id": request_id, "ok": False, "error": "本机观察器内部错误"}


def read_native_message(stream: BinaryIO) -> Any | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ProtocolError("Native Messaging 帧头不完整")
    (length,) = struct.unpack("=I", header)
    if length == 0 or length > MAX_NATIVE_MESSAGE_BYTES:
        raise ProtocolError("Native Messaging 消息大小无效")
    payload = stream.read(length)
    if len(payload) != length:
        raise ProtocolError("Native Messaging 消息不完整")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("Native Messaging 消息不是有效 JSON") from error


def write_native_message(stream: BinaryIO, message: Mapping[str, Any]) -> None:
    payload = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_NATIVE_MESSAGE_BYTES:
        payload = b'{"request_id":"invalid","ok":false,"error":"response too large"}'
    stream.write(struct.pack("=I", len(payload)))
    stream.write(payload)
    stream.flush()


def run_native_host(config_path: Path | None = None) -> int:
    input_stream = sys.stdin.buffer
    output_stream = sys.stdout.buffer
    host = NativeHost(config_path)

    while True:
        try:
            message = read_native_message(input_stream)
        except HostError as error:
            write_native_message(
                output_stream,
                {"request_id": "invalid", "ok": False, "error": str(error)},
            )
            return 2
        if message is None:
            return 0
        write_native_message(output_stream, host.handle(message))


def read_pairing_code(path: Path | None, from_stdin: bool) -> str:
    if path and from_stdin:
        raise ConfigError("只能选择一种配对码输入方式")
    if path:
        ensure_secure_regular_file(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigError("无法读取配对码文件") from error
    elif from_stdin:
        raw = sys.stdin.readline()
    else:
        raise ConfigError("缺少配对码")
    return validate_secret(raw.strip(), "pairing_code")


def write_initial_config(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else default_config_path()
    pairing_code = read_pairing_code(
        Path(args.pairing_code_file).expanduser() if args.pairing_code_file else None,
        args.pairing_code_stdin,
    )
    write_config_file(
        path,
        {
            "schema_version": 1,
            "api_base": validate_api_base(args.api_base),
            "source_id": validate_source_id(args.source_id),
            "pairing_code": pairing_code,
        },
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CentaurAI 微信网页观察器 Native Host")
    parser.add_argument("--config", help="覆盖默认配置文件路径")
    parser.add_argument("--write-config", action="store_true", help="安全写入初始配对配置")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--source-id")
    pairing = parser.add_mutually_exclusive_group()
    pairing.add_argument("--pairing-code-file")
    pairing.add_argument("--pairing-code-stdin", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.write_config:
            if not args.source_id:
                raise ConfigError("--source-id 不能为空")
            return write_initial_config(args)
        if args.source_id or args.pairing_code_file or args.pairing_code_stdin:
            raise ConfigError("配置参数只能与 --write-config 一起使用")
        return run_native_host(Path(args.config).expanduser() if args.config else None)
    except HostError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

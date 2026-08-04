from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import struct
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "native_host.py"
SPEC = importlib.util.spec_from_file_location("centaur_wechat_native_host", MODULE_PATH)
assert SPEC and SPEC.loader
host_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = host_module
SPEC.loader.exec_module(host_module)


def handshake_body() -> dict:
    return {
        "extension_id": host_module.EXTENSION_ID,
        "extension_version": "0.1.0",
        "browser_name": "firefox",
        "browser_version": "128.0",
        "parser_version": "visible-dom-v1",
    }


def heartbeat_body() -> dict:
    return {
        "browser_session_id": "session-1",
        "state": "active",
        "observed_at": "2026-07-31T12:00:00Z",
        "extension_version": "0.1.0",
        "parser_version": "visible-dom-v1",
        "current_conversation_id": "@@leadership-room",
        "current_conversation_name": "管理层例会",
        "unread_conversation_count": 2,
    }


def events_body() -> dict:
    return {
        "batch_id": "batch-1",
        "browser_session_id": "session-1",
        "events": [
            {
                "provider_msgid": "m-1001",
                "provider_conversation_id": "@@leadership-room",
                "conversation_name": "管理层例会",
                "conversation_type": "group",
                "direction": "incoming",
                "message_type": "text",
                "sender_provider_id": "@alice",
                "sender_display_name": "Alice",
                "text": "明天 10:00 开会",
                "displayed_time_text": "昨天 18:30",
                "observed_at": "2026-07-31T12:00:00Z",
            }
        ],
    }


class RecordingHandler(BaseHTTPRequestHandler):
    records: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.__class__.records.append(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
        )
        if self.path.endswith("/handshake"):
            response = {"source_id": "src_test", "collector_token": "collector-secret", "token_type": "Bearer"}
            status = 201
        elif self.path.endswith("/heartbeat"):
            response = {"ok": True}
            status = 200
        else:
            response = {"batch_id": "batch-1", "accepted_count": 1, "duplicate_count": 0, "total_count": 1}
            status = 202
        encoded = json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class NativeHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_path = Path(self.temporary.name) / "wechat-observer.json"

    def write_pairing_config(self, api_base: str) -> None:
        host_module.write_config_file(
            self.config_path,
            {
                "schema_version": 1,
                "api_base": api_base,
                "source_id": "src_test",
                "pairing_code": "pairing-secret",
            },
        )

    def test_loopback_url_validation_rejects_ssrf_variants(self) -> None:
        self.assertEqual(host_module.validate_api_base("http://127.0.0.1:8718"), "http://127.0.0.1:8718")
        for unsafe in (
            "http://localhost:8718",
            "http://127.0.0.2:8718",
            "https://127.0.0.1:8718",
            "http://user@127.0.0.1:8718",
            "http://127.0.0.1:8718/api",
            "http://127.0.0.1:8718?target=other",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(host_module.ConfigError):
                host_module.validate_api_base(unsafe)

    def test_config_is_private_and_rejects_symlinks(self) -> None:
        self.write_pairing_config("http://127.0.0.1:8718")
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        loaded = host_module.HostConfig.load(self.config_path)
        self.assertEqual(loaded.pairing_code, "pairing-secret")

        link = self.config_path.parent / "linked.json"
        link.symlink_to(self.config_path)
        with self.assertRaises(host_module.ConfigError):
            host_module.HostConfig.load(link)

    def test_schema_rejects_extra_fields_wrong_extension_and_unknown_type(self) -> None:
        bad_handshake = handshake_body() | {"owner_token": "must-not-pass"}
        with self.assertRaises(host_module.ProtocolError):
            host_module.validate_handshake(bad_handshake)

        wrong_extension = handshake_body() | {"extension_id": "attacker@example.invalid"}
        with self.assertRaises(host_module.ProtocolError):
            host_module.validate_handshake(wrong_extension)

        batch = events_body()
        batch["events"][0]["message_type"] = "wechat_private_type"
        with self.assertRaises(host_module.ProtocolError):
            host_module.validate_events(batch)

    def test_native_message_framing_round_trip_and_size_limit(self) -> None:
        message = {"type": "heartbeat", "request_id": "request-1", "body": heartbeat_body()}
        stream = io.BytesIO()
        host_module.write_native_message(stream, message)
        stream.seek(0)
        self.assertEqual(host_module.read_native_message(stream), message)

        oversized = io.BytesIO(struct.pack("=I", host_module.MAX_NATIVE_MESSAGE_BYTES + 1))
        with self.assertRaises(host_module.ProtocolError):
            host_module.read_native_message(oversized)

    def test_rate_limiter_counts_requests_and_events(self) -> None:
        limiter = host_module.SlidingWindowLimiter(max_messages=2, max_events=2, window_seconds=60)
        limiter.check(1, now=100)
        limiter.check(1, now=101)
        with self.assertRaises(host_module.ProtocolError):
            limiter.check(0, now=102)
        limiter.check(1, now=161)

    def test_configure_handshake_heartbeat_and_events_use_separate_tokens(self) -> None:
        RecordingHandler.records = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        native = host_module.NativeHost(self.config_path)
        configure = native.handle(
            {
                "type": "configure",
                "request_id": "configure-1",
                "body": {
                    "extension_id": host_module.EXTENSION_ID,
                    "api_base": f"http://127.0.0.1:{server.server_port}",
                    "source_id": "src_test",
                    "pairing_code": "pairing-secret",
                },
            }
        )
        self.assertTrue(configure["ok"])

        paired = native.handle(
            {"type": "handshake", "request_id": "handshake-1", "body": handshake_body()}
        )
        self.assertTrue(paired["ok"])
        self.assertNotIn("collector_token", paired)

        heartbeat = native.handle(
            {"type": "heartbeat", "request_id": "heartbeat-1", "body": heartbeat_body()}
        )
        self.assertTrue(heartbeat["ok"])
        events = native.handle(
            {"type": "events", "request_id": "events-1", "body": events_body()}
        )
        self.assertTrue(events["ok"])

        self.assertEqual(len(RecordingHandler.records), 3)
        self.assertEqual(RecordingHandler.records[0]["authorization"], "Bearer pairing-secret")
        self.assertEqual(RecordingHandler.records[1]["authorization"], "Bearer collector-secret")
        self.assertEqual(RecordingHandler.records[2]["authorization"], "Bearer collector-secret")
        self.assertTrue(RecordingHandler.records[0]["path"].endswith("/handshake"))
        self.assertTrue(RecordingHandler.records[1]["path"].endswith("/heartbeat"))
        self.assertTrue(RecordingHandler.records[2]["path"].endswith("/events"))

        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("pairing_code", persisted)
        self.assertEqual(persisted["collector_token"], "collector-secret")
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

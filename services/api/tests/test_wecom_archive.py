from __future__ import annotations

from centaur_pocket.wecom_archive import (
    NativeWeComArchiveSDK,
    WeComArchiveCollector,
    WeComArchiveError,
    normalize_wecom_message,
)


class FixtureSDK:
    def __init__(self) -> None:
        self.page = {
            "errcode": 0,
            "errmsg": "ok",
            "chatdata": [
                {
                    "seq": 41,
                    "publickey_ver": 3,
                    "encrypt_random_key": "key-a",
                    "encrypt_chat_msg": "message-a",
                },
                {
                    "seq": 42,
                    "publickey_ver": 4,
                    "encrypt_random_key": "key-b",
                    "encrypt_chat_msg": "message-b",
                },
            ],
        }

    def get_chat_data(self, *, seq: int, limit: int) -> dict:
        assert seq == 40
        assert limit == 2
        return self.page

    def decrypt_data(
        self, *, decrypted_random_key: str, encrypted_message: str
    ) -> dict:
        assert decrypted_random_key.startswith("decrypted-")
        suffix = decrypted_random_key[-1]
        return {
            "msgid": f"msg-{suffix}",
            "action": "send",
            "from": "owner" if suffix == "a" else "external",
            "tolist": ["external" if suffix == "a" else "owner"],
            "msgtime": 1_700_000_000_000,
            "msgtype": "text",
            "text": {"content": "收到"},
        }

    def close(self) -> None:
        pass


def test_page_keeps_equal_text_as_distinct_provider_messages() -> None:
    decrypted: list[tuple[str, int]] = []

    def decrypt_random_key(
        *, encrypted_random_key: str, public_key_version: int
    ) -> str:
        decrypted.append((encrypted_random_key, public_key_version))
        return f"decrypted-{encrypted_random_key[-1]}"

    collector = WeComArchiveCollector(
        FixtureSDK(),
        archived_member_ids={"owner"},
        decrypt_random_key=decrypt_random_key,
    )

    page = collector.pull_page(seq=40, limit=2)

    assert page.next_seq == 42
    assert page.has_more is True
    assert [event["provider_msgid"] for event in page.events] == ["msg-a", "msg-b"]
    assert [event["text"] for event in page.events] == ["收到", "收到"]
    assert [event["direction"] for event in page.events] == ["outgoing", "incoming"]
    assert decrypted == [("key-a", 3), ("key-b", 4)]


def test_normalizer_preserves_group_media_and_retraction_evidence() -> None:
    event = normalize_wecom_message(
        {
            "msgid": "group-1",
            "action": "recall",
            "from": "member-a",
            "tolist": ["owner"],
            "roomid": "room-7",
            "msgtime": 1_700_000_000_000,
            "msgtype": "file",
            "file": {"filename": "合同.pdf", "sdkfileid": "media-1"},
        },
        source_seq=7,
        archived_member_ids={"owner"},
    )

    assert event["provider_conversation_id"] == "room-7"
    assert event["conversation_type"] == "group"
    assert event["action"] == "recall"
    assert event["media_references"] == ["media-1"]
    assert event["source_seq"] == 7


def test_invalid_sdk_envelope_fails_closed() -> None:
    sdk = FixtureSDK()
    sdk.page = {"errcode": 10001, "errmsg": "permission denied"}
    collector = WeComArchiveCollector(
        sdk,
        archived_member_ids={"owner"},
        decrypt_random_key=lambda **_kwargs: "unused",
    )

    try:
        collector.pull_page(seq=40, limit=2)
    except WeComArchiveError as error:
        assert "permission denied" in str(error)
    else:
        raise AssertionError("invalid SDK response should fail")


def test_native_decrypt_uses_the_official_three_argument_signature() -> None:
    calls: list[tuple[bytes, bytes, int]] = []

    class FakeLibrary:
        @staticmethod
        def NewSlice() -> int:
            return 17

        @staticmethod
        def FreeSlice(_output: int) -> None:
            return None

        @staticmethod
        def GetContentFromSlice(_output: int) -> bytes:
            return b'{"msgid":"message-1"}'

        @staticmethod
        def DecryptData(key: bytes, message: bytes, output: int) -> int:
            calls.append((key, message, output))
            return 0

    sdk = NativeWeComArchiveSDK.__new__(NativeWeComArchiveSDK)
    sdk._library = FakeLibrary()
    sdk._sdk = 999

    result = sdk.decrypt_data(
        decrypted_random_key="plain-random-key",
        encrypted_message="encrypted-message",
    )

    assert result == {"msgid": "message-1"}
    assert calls == [(b"plain-random-key", b"encrypted-message", 17)]

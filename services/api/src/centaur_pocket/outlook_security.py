from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
import threading
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

OUTLOOK_KEY_FILE = "outlook-token-key"
OUTLOOK_KEY_BYTES = 32
MAX_CIPHERTEXT_BYTES = 256 * 1024
MAX_ENCRYPTED_ATTACHMENT_BYTES = 32 * 1024 * 1024
MICROSOFT_DEVICE_LOGIN_HOST = "microsoft.com"
MICROSOFT_DEVICE_LOGIN_PATH = "/devicelogin"
SUPPORTED_TENANTS = {"common", "organizations", "consumers"}


class OutlookSecurityError(RuntimeError):
    """A fail-closed local credential or external-URL validation error."""


def normalize_outlook_client_id(value: str) -> str:
    try:
        parsed = UUID(value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError("client_id 必须是 Microsoft 应用注册的 UUID") from error
    if parsed.int == 0:
        raise ValueError("client_id 不能是空 UUID")
    return str(parsed)


def normalize_outlook_tenant(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in SUPPORTED_TENANTS:
        return normalized
    try:
        parsed = UUID(normalized)
    except ValueError as error:
        raise ValueError(
            "tenant 只能是 common、organizations、consumers 或租户 UUID"
        ) from error
    if parsed.int == 0:
        raise ValueError("tenant 不能是空 UUID")
    return str(parsed)


def validate_microsoft_verification_uri(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as error:
        raise OutlookSecurityError("Microsoft 登录地址格式无效") from error
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or hostname != MICROSOFT_DEVICE_LOGIN_HOST
        or path != MICROSOFT_DEVICE_LOGIN_PATH
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or parsed.query
    ):
        raise OutlookSecurityError("Microsoft 登录地址不在允许范围内")
    return urlunsplit(
        ("https", MICROSOFT_DEVICE_LOGIN_HOST, MICROSOFT_DEVICE_LOGIN_PATH, "", "")
    )


def validate_graph_delta_url(value: str) -> str:
    """Accept only opaque Inbox delta continuations issued by Microsoft Graph."""

    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as error:
        raise OutlookSecurityError("Outlook 增量游标格式无效") from error
    path = parsed.path
    normalized_path = path.casefold().replace("%27", "'")
    # Graph normally preserves the well-known Inbox name in nextLink/deltaLink.
    # Fail closed if it ever rewrites the folder to an opaque ID: accepting an
    # arbitrary folder ID here would silently widen this connector beyond Inbox.
    is_inbox_delta = normalized_path in {
        "/v1.0/me/mailfolders/inbox/messages/delta",
        "/v1.0/me/mailfolders('inbox')/messages/delta",
    }
    if (
        parsed.scheme != "https"
        or hostname != "graph.microsoft.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or not is_inbox_delta
        or not parsed.query
        or len(value) > 16_384
    ):
        raise OutlookSecurityError("Outlook 增量游标不在允许范围内")
    return urlunsplit(("https", "graph.microsoft.com", path, parsed.query, ""))


def sanitize_outlook_text(value: object, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    cleaned = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"}
        or not unicodedata.category(character).startswith("C")
    )
    return cleaned[:max_chars].strip()


class OutlookSecretBox:
    """Encrypt OAuth state and mailbox cursors with a private local AES-GCM key."""

    def __init__(self, data_root: Path):
        self._data_root = data_root
        self._key_path = data_root / OUTLOOK_KEY_FILE
        self._key: bytes | None = None
        self._lock = threading.Lock()

    @property
    def key_path(self) -> Path:
        return self._key_path

    def encrypt_text(self, purpose: str, record_id: str, value: str) -> str:
        plaintext = value.encode("utf-8")
        if len(plaintext) > MAX_CIPHERTEXT_BYTES:
            raise OutlookSecurityError("Outlook 私密状态超过本地安全上限")
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._load_or_create_key()).encrypt(
            nonce,
            plaintext,
            self._aad(purpose, record_id),
        )
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt_text(self, purpose: str, record_id: str, value: str) -> str:
        try:
            raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeEncodeError, ValueError) as error:
            raise OutlookSecurityError("Outlook 私密状态无法解密") from error
        if len(raw) < 29 or len(raw) > MAX_CIPHERTEXT_BYTES + 28:
            raise OutlookSecurityError("Outlook 私密状态无法解密")
        try:
            plaintext = AESGCM(self._load_existing_key()).decrypt(
                raw[:12],
                raw[12:],
                self._aad(purpose, record_id),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as error:
            raise OutlookSecurityError("Outlook 私密状态无法解密") from error

    def encrypt_bytes(self, purpose: str, record_id: str, value: bytes) -> bytes:
        if len(value) > MAX_ENCRYPTED_ATTACHMENT_BYTES:
            raise OutlookSecurityError("Outlook 归档内容超过本地安全上限")
        nonce = os.urandom(12)
        return nonce + AESGCM(self._load_or_create_key()).encrypt(
            nonce,
            value,
            self._aad(purpose, record_id),
        )

    def decrypt_bytes(self, purpose: str, record_id: str, value: bytes) -> bytes:
        if len(value) < 29 or len(value) > MAX_ENCRYPTED_ATTACHMENT_BYTES + 28:
            raise OutlookSecurityError("Outlook 归档内容无法解密")
        try:
            return AESGCM(self._load_existing_key()).decrypt(
                value[:12],
                value[12:],
                self._aad(purpose, record_id),
            )
        except InvalidTag as error:
            raise OutlookSecurityError("Outlook 归档内容无法解密") from error

    def opaque_reference(self, purpose: str, *values: str) -> str:
        payload = "\x1f".join((purpose, *values)).encode("utf-8")
        digest = hmac.new(
            self._load_or_create_key(), payload, hashlib.sha256
        ).hexdigest()
        return digest[:40]

    def opaque_reference_existing(self, purpose: str, *values: str) -> str:
        """Derive a reference without ever creating a replacement key."""

        payload = "\x1f".join((purpose, *values)).encode("utf-8")
        return hmac.new(
            self._load_existing_key(), payload, hashlib.sha256
        ).hexdigest()[:40]

    @staticmethod
    def _aad(purpose: str, record_id: str) -> bytes:
        if not purpose or not record_id or len(purpose) > 100 or len(record_id) > 300:
            raise OutlookSecurityError("Outlook 私密状态标识无效")
        return f"centaurai-pocket:outlook:v1:{purpose}:{record_id}".encode()

    def _load_or_create_key(self) -> bytes:
        with self._lock:
            if self._key is not None:
                return self._key
            self._data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self._data_root.chmod(0o700)
            except OSError:
                pass
            try:
                descriptor = os.open(
                    self._key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                self._key = self._read_valid_key()
                return self._key
            key = os.urandom(OUTLOOK_KEY_BYTES)
            try:
                written = 0
                while written < len(key):
                    written += os.write(descriptor, key[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._key = self._read_valid_key()
            return self._key

    def _load_existing_key(self) -> bytes:
        with self._lock:
            if self._key is None:
                self._key = self._read_valid_key()
            return self._key

    def _read_valid_key(self) -> bytes:
        try:
            metadata = self._key_path.lstat()
        except OSError as error:
            raise OutlookSecurityError("Outlook 本地加密密钥不可用") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size != OUTLOOK_KEY_BYTES
        ):
            raise OutlookSecurityError("Outlook 本地加密密钥权限或格式无效")
        try:
            descriptor = os.open(
                self._key_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                key = os.read(descriptor, OUTLOOK_KEY_BYTES + 1)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise OutlookSecurityError("Outlook 本地加密密钥不可用") from error
        if len(key) != OUTLOOK_KEY_BYTES:
            raise OutlookSecurityError("Outlook 本地加密密钥格式无效")
        return key

from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTION_PREFIX = "enc:v1:"


class EncryptionError(RuntimeError):
    """Raised when encrypted data cannot be decoded."""


def encrypt_value(value: Optional[str], key: Optional[str]) -> Optional[str]:
    """Encrypt plaintext with enc:v1 prefix, leaving empty/already encrypted values intact."""

    if value is None or not key or value.startswith(ENCRYPTION_PREFIX):
        return value
    return ENCRYPTION_PREFIX + _fernet(key).encrypt(value.encode()).decode()


def decrypt_value(value: Optional[str], key: Optional[str]) -> Optional[str]:
    """Decrypt enc:v1 values and leave plaintext compatibility values untouched."""

    if value is None or not value.startswith(ENCRYPTION_PREFIX):
        return value
    if not key:
        raise EncryptionError("Поле зашифровано, но VPN_BOT_FIELD_ENCRYPTION_KEY не настроен.")
    token = value.removeprefix(ENCRYPTION_PREFIX)
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise EncryptionError("Не удалось расшифровать значение.") from exc


def _fernet(key: str) -> Fernet:
    """Build a Fernet instance from a Fernet key or derive one from passphrase text."""

    raw = key.strip().encode()
    try:
        return Fernet(raw)
    except ValueError:
        digest = hashlib.sha256(raw).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

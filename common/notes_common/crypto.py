"""Encrypts the user's Notion token while it sits in the queue.

The gateway never stores the token (spec section 5); it only travels inside the job
message, Fernet-encrypted, and is decrypted by the worker that processes the job.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings
from .errors import PermanentError


def _fernet() -> Fernet:
    key = get_settings().token_enc_key
    if not key:
        raise RuntimeError("TOKEN_ENC_KEY is not configured")
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise PermanentError("Stored Notion credentials could not be decrypted", "TOKEN_DECRYPT_FAILED") from exc

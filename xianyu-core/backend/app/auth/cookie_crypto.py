"""Cookie AES-256-GCM 加密（参考 xianyu-pilot cookie_crypto.py，盘点 ★5）。

格式：enc:v1:{b64url(iv)}:{b64url(ct+tag)}
密钥 = SHA256(COOKIE_CRYPTO_SECRET)。
"""

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "enc:v1:"
_IV_LEN = 12


def _derive_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def encrypt(plaintext: str, secret: str) -> str:
    key = _derive_key(secret)
    iv = os.urandom(_IV_LEN)
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return f"{_PREFIX}{_b64url(iv)}:{_b64url(ct)}"


def decrypt(encrypted: str, secret: str) -> str:
    if not encrypted.startswith(_PREFIX):
        raise ValueError("不是 enc:v1: 格式的密文")
    iv_part, ct_part = encrypted[len(_PREFIX):].split(":", 1)
    key = _derive_key(secret)
    plaintext = AESGCM(key).decrypt(_b64url_decode(iv_part), _b64url_decode(ct_part), None)
    return plaintext.decode("utf-8")


def is_encrypted(value: str) -> bool:
    return value.startswith(_PREFIX)

"""
oneframe_cloud/bundle.py  -- authenticated encryption for staged media + manifests.

AES-256-GCM via the `cryptography` package (AEAD; no home-grown crypto). A fresh
random 96-bit nonce per seal. The associated data binds every bundle to its
item identity, so a bundle renamed / swapped onto another queue item fails to
open.

  media bundle   b"OFCB1" | nonce(12) | GCM( u32 header_len | header JSON | MP4 bytes )
                 AAD = "OFCB1|<item_ref>|<source_sha256>"
  manifest       b"OFCM1" | nonce(12) | GCM( JSON )
                 AAD = "OFCM1|<queue_ref>"

The key is 32 random bytes, base64 (ONEFRAME_MEDIA_KEY_B64). It lives only in
the local gitignored .env and in GitHub repository Secrets.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os

MEDIA_MAGIC = b"OFCB1"
MANIFEST_MAGIC = b"OFCM1"
NONCE_LEN = 12


class BundleError(Exception):
    """code: WRONG_KEY_OR_TAMPERED | SOURCE_SHA256_MISMATCH | IDENTITY_MISMATCH | BAD_FORMAT | BAD_KEY"""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # pinned dependency
    return AESGCM(key)


def key_from_b64(s: str) -> bytes:
    try:
        key = base64.b64decode((s or "").strip(), validate=True)
    except (binascii.Error, ValueError):
        raise BundleError("BAD_KEY", "media key is not valid base64") from None
    if len(key) != 32:
        raise BundleError("BAD_KEY", "media key must be 32 bytes (AES-256)")
    return key


def new_key_b64() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def key_id(key: bytes) -> str:
    """Public, non-reversible fingerprint: detects a wrong key before decrypting."""
    return hashlib.sha256(b"ONEFRAME-CLOUD-KEY-ID|" + key).hexdigest()[:16]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _media_aad(item_ref: str, source_sha256: str) -> bytes:
    return f"OFCB1|{item_ref}|{source_sha256}".encode()


def seal_media(key: bytes, *, item_ref: str, source_sha256: str, mp4: bytes,
               identity: dict) -> bytes:
    if sha256_bytes(mp4) != source_sha256:
        raise BundleError("SOURCE_SHA256_MISMATCH", "refusing to seal: bytes != SOURCE_SHA256")
    ident = dict(identity, item_ref=item_ref, source_sha256=source_sha256)
    header = json.dumps(ident, ensure_ascii=False, sort_keys=True).encode()
    payload = len(header).to_bytes(4, "big") + header + mp4
    nonce = os.urandom(NONCE_LEN)
    return MEDIA_MAGIC + nonce + _aesgcm(key).encrypt(nonce, payload,
                                                     _media_aad(item_ref, source_sha256))


def open_media(key: bytes, blob: bytes, *, item_ref: str, source_sha256: str):
    """-> (identity dict, mp4 bytes). Fails closed on any mismatch."""
    from cryptography.exceptions import InvalidTag
    if not blob.startswith(MEDIA_MAGIC) or len(blob) < len(MEDIA_MAGIC) + NONCE_LEN + 16:
        raise BundleError("BAD_FORMAT", "not an OFCB1 media bundle")
    nonce = blob[len(MEDIA_MAGIC):len(MEDIA_MAGIC) + NONCE_LEN]
    try:
        payload = _aesgcm(key).decrypt(nonce, blob[len(MEDIA_MAGIC) + NONCE_LEN:],
                                       _media_aad(item_ref, source_sha256))
    except InvalidTag:
        raise BundleError("WRONG_KEY_OR_TAMPERED",
                          "authentication failed (wrong key, wrong item, or modified bundle)") from None
    hl = int.from_bytes(payload[:4], "big")
    ident = json.loads(payload[4:4 + hl].decode())
    mp4 = payload[4 + hl:]
    if ident.get("item_ref") != item_ref or ident.get("source_sha256") != source_sha256:
        raise BundleError("IDENTITY_MISMATCH", "bundle identity != queue item")
    if sha256_bytes(mp4) != source_sha256:
        raise BundleError("SOURCE_SHA256_MISMATCH", "decrypted bytes != SOURCE_SHA256")
    return ident, mp4


def seal_json(key: bytes, *, queue_ref: str, obj) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()
    return MANIFEST_MAGIC + nonce + _aesgcm(key).encrypt(nonce, data,
                                                        f"OFCM1|{queue_ref}".encode())


def open_json(key: bytes, blob: bytes, *, queue_ref: str):
    from cryptography.exceptions import InvalidTag
    if not blob.startswith(MANIFEST_MAGIC):
        raise BundleError("BAD_FORMAT", "not an OFCM1 manifest")
    nonce = blob[len(MANIFEST_MAGIC):len(MANIFEST_MAGIC) + NONCE_LEN]
    try:
        data = _aesgcm(key).decrypt(nonce, blob[len(MANIFEST_MAGIC) + NONCE_LEN:],
                                    f"OFCM1|{queue_ref}".encode())
    except InvalidTag:
        raise BundleError("WRONG_KEY_OR_TAMPERED", "manifest authentication failed") from None
    return json.loads(data.decode())

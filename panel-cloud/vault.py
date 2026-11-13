"""Encryption for stored Speexx passwords.

Until now the panel never persisted a Speexx password at all: credentials arrived
on stdin and were scrubbed out of everything written to disk. That was possible
because a job was one login for one run. Saving several accounts per person
breaks that, so the secret has to be stored — encrypted, here.

AES-256-GCM from `cryptography` (pyca). GCM is authenticated, which means a
tampered ciphertext fails to decrypt rather than yielding garbage. Each record
uses a fresh random 96-bit nonce, and the owner is bound in as associated data,
so a ciphertext cannot be moved from one row to another and still decrypt.

The associated data is `user_id:email`, not the row id, because it has to be
knowable *before* the insert that would assign the row id.

The server key lives in `data/vault.key`, mode 0600, generated on first use.
**Losing it makes every saved account unrecoverable** — the user re-enters their
Speexx passwords, nothing else is affected. It is deliberately not in the
database, so a stolen panel.db alone yields no plaintext.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DATA_DIR = Path(
    os.environ.get("SPEEXX_PANEL_DATA_DIR")
    or (Path(__file__).resolve().parent / "data")
)
KEY_PATH = DATA_DIR / "vault.key"

NONCE_BYTES = 12


class VaultError(Exception):
    """Key missing, wrong, or the record does not belong to this account."""


def _load_key() -> bytes:
    env = os.environ.get("SPEEXX_PANEL_VAULT_KEY")
    if env:
        try:
            key = bytes.fromhex(env)
        except ValueError:
            raise VaultError("SPEEXX_PANEL_VAULT_KEY must be hex")
        if len(key) != 32:
            raise VaultError("SPEEXX_PANEL_VAULT_KEY must be 32 bytes (64 hex chars)")
        return key
    if not KEY_PATH.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DATA_DIR, 0o700)
        key = AESGCM.generate_key(bit_length=256)
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        return key
    key = KEY_PATH.read_bytes()
    if len(key) != 32:
        raise VaultError("vault.key is not a 32-byte key; move it aside to start over")
    return key


def key_present() -> bool:
    return bool(os.environ.get("SPEEXX_PANEL_VAULT_KEY")) or KEY_PATH.exists()


def aad_for(user_id: int, email: str) -> bytes:
    """Bind a ciphertext to its owner and address, both known before insert."""
    return ("speexx-account:%s:%s" % (int(user_id), (email or "").strip().lower())).encode()


def encrypt(plaintext: str, user_id: int, email: str) -> bytes:
    """Return nonce || ciphertext for this owner/address pair."""
    cipher = AESGCM(_load_key())
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + cipher.encrypt(nonce, plaintext.encode("utf-8"), aad_for(user_id, email))


def decrypt(blob: bytes, user_id: int, email: str) -> str:
    if not blob or len(blob) <= NONCE_BYTES:
        raise VaultError("empty or truncated record")
    cipher = AESGCM(_load_key())
    nonce, body = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return cipher.decrypt(nonce, body, aad_for(user_id, email)).decode("utf-8")
    except InvalidTag:
        # Wrong key, tampered bytes, or a record moved between rows. All three
        # are indistinguishable here on purpose.
        raise VaultError("cannot decrypt this record")
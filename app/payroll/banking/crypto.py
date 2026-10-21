"""Envelope encryption and keyed fingerprints for bank details (3.7 §12, 3.9 §9, decision I-10).

A per-tenant 256-bit data key (DEK) encrypts account numbers, IBANs and sensitive routing data
with AES-256-GCM. The DEK is stored only wrapped, under a key-encryption key derived (HKDF-SHA256)
from the platform master key, so database backups never contain a usable key or a plain number.
Every ciphertext names the key that sealed it, which makes rotation possible without downtime:

    v1:<key_id>:<base64 nonce>:<base64 ciphertext and tag>

``rewrap`` moves a wrapped DEK to a new master key (EC-SEC-15); ``reencrypt`` moves a ciphertext to
a new DEK. The caller's associated data (for example ``b"bank_accounts.iban:12:345"``) and the
envelope header are authenticated, so a ciphertext copied to another row, column or tenant, or a
header pointed at another key, fails to decrypt. Nonces are 96 random bits per encryption.

Duplicate accounts are detected without decrypting (3.7 §3.5): ``fingerprint`` is HMAC-SHA256 over
the normalised account identity under a per-tenant key from ``derive_fingerprint_key``, so equal
accounts collide only inside one tenant and a database reader cannot test guesses against the
stored values. Keys, plaintext and ciphertext never appear in errors or reprs.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.payroll.banking.errors import CryptoError

VERSION = "v1"
FINGERPRINT_VERSION = "v1"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16

_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_IDENTITY_PART = re.compile(r"[A-Z0-9]*")
_KEK_SALT = b"hrms.payroll.key-encryption-key"
_FINGERPRINT_SALT = b"hrms.payroll.bank-fingerprint"
_DATA_CONTEXT = b"data\x00"
_DEK_CONTEXT = b"dek\x00"


@dataclass(frozen=True, repr=False)
class DataKey:
    """A tenant data key; ``material`` never appears in ``repr``."""

    key_id: str
    material: bytes

    def __post_init__(self) -> None:
        _check_key_id(self.key_id)
        _check_key(self.material, "data key")

    def __repr__(self) -> str:
        return f"DataKey(key_id={self.key_id!r})"


def _check_key_id(key_id: str) -> None:
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise CryptoError("INVALID_KEY_ID", "key ids are 1-64 letters, digits, '.', '_' or '-' and start with a letter or digit")


def _check_key(key: bytes, label: str) -> None:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise CryptoError("INVALID_KEY", f"the {label} must be {KEY_BYTES} bytes")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise CryptoError("MALFORMED_ENVELOPE", "the ciphertext is not valid base64") from None
    if _b64(data) != text:
        raise CryptoError("MALFORMED_ENVELOPE", "the ciphertext is not canonical base64")
    return data


def _parse(envelope: str) -> tuple[str, bytes, bytes]:
    if not isinstance(envelope, str):
        raise CryptoError("MALFORMED_ENVELOPE", "the ciphertext must be text")
    parts = envelope.split(":")
    if len(parts) != 4:
        raise CryptoError("MALFORMED_ENVELOPE", "expected version:key_id:nonce:ciphertext")
    version, key_id, nonce_text, sealed_text = parts
    if version != VERSION:
        raise CryptoError("UNSUPPORTED_VERSION", "unsupported ciphertext version")
    _check_key_id(key_id)
    nonce, sealed = _unb64(nonce_text), _unb64(sealed_text)
    if len(nonce) != NONCE_BYTES or len(sealed) < TAG_BYTES:
        raise CryptoError("MALFORMED_ENVELOPE", "the nonce or ciphertext has the wrong length")
    return key_id, nonce, sealed


def _associated(key_id: str, context: bytes) -> bytes:
    return f"{VERSION}:{key_id}:".encode("ascii") + context


def _seal(key: bytes, key_id: str, plaintext: bytes, context: bytes, nonce: bytes) -> str:
    sealed = AESGCM(key).encrypt(nonce, plaintext, _associated(key_id, context))
    return f"{VERSION}:{key_id}:{_b64(nonce)}:{_b64(sealed)}"


def _open(key: bytes, envelope: str, context: bytes) -> tuple[str, bytes]:
    key_id, nonce, sealed = _parse(envelope)
    try:
        return key_id, AESGCM(key).decrypt(nonce, sealed, _associated(key_id, context))
    except InvalidTag:
        raise CryptoError(
            "DECRYPT_FAILED", "the ciphertext failed authentication (wrong key or associated data, or it was altered)"
        ) from None


def _kek(master_key: bytes) -> bytes:
    _check_key(master_key, "master key")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=_KEK_SALT, info=VERSION.encode()).derive(master_key)


def _aad(aad: bytes) -> bytes:
    if not isinstance(aad, bytes):
        raise CryptoError("INVALID_ARGUMENT", "associated data must be bytes")
    return _DATA_CONTEXT + aad


def load_master_key(encoded: str) -> bytes:
    """Decode a base64 master key (for example from a secrets manager) and check it is 256 bits."""
    try:
        key = base64.b64decode("".join(encoded.split()).encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError, AttributeError):
        raise CryptoError("INVALID_KEY", "the master key is not valid base64") from None
    _check_key(key, "master key")
    return key


def generate_data_key(key_id: str | None = None) -> DataKey:
    """A new random 256-bit data key; the id defaults to ``dk-<16 hex>``."""
    return DataKey(key_id or f"dk-{secrets.token_hex(8)}", AESGCM.generate_key(bit_length=256))


def wrap_key(master_key: bytes, dek: DataKey, key_id: str) -> str:
    """Seal a data key under the master key ``key_id``; the result names that master key."""
    _check_key_id(key_id)
    nonce = secrets.token_bytes(NONCE_BYTES)
    return _seal(_kek(master_key), key_id, dek.material, _DEK_CONTEXT + dek.key_id.encode("ascii"), nonce)


def unwrap_key(master_key: bytes, wrapped: str, dek_key_id: str) -> DataKey:
    """Recover the data key ``dek_key_id`` from its wrapped form (fails if it was wrapped for another id)."""
    _check_key_id(dek_key_id)
    _, material = _open(_kek(master_key), wrapped, _DEK_CONTEXT + dek_key_id.encode("ascii"))
    return DataKey(dek_key_id, material)


def rewrap(wrapped: str, *, dek_key_id: str, old_master_key: bytes, new_master_key: bytes, new_key_id: str) -> str:
    """Master-key rotation: re-seal a wrapped data key under a new master key; data stays untouched."""
    return wrap_key(new_master_key, unwrap_key(old_master_key, wrapped, dek_key_id), new_key_id)


def encrypt(dek: DataKey, plaintext: str, aad: bytes) -> str:
    """``"v1:<dek.key_id>:<nonce>:<ciphertext>"`` for a text value bound to ``aad``."""
    if not isinstance(dek, DataKey):
        raise CryptoError("INVALID_KEY", "encrypt needs a DataKey")
    if not isinstance(plaintext, str):
        raise CryptoError("INVALID_ARGUMENT", "only text values are encrypted")
    nonce = secrets.token_bytes(NONCE_BYTES)
    return _seal(dek.material, dek.key_id, plaintext.encode("utf-8"), _aad(aad), nonce)


def decrypt(dek: DataKey, envelope: str, aad: bytes) -> str:
    """The text sealed by ``encrypt`` with the same data key and associated data."""
    if not isinstance(dek, DataKey):
        raise CryptoError("INVALID_KEY", "decrypt needs a DataKey")
    if envelope_key_id(envelope) != dek.key_id:
        raise CryptoError("KEY_ID_MISMATCH", "the ciphertext was sealed with another data key")
    _, plaintext = _open(dek.material, envelope, _aad(aad))
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        raise CryptoError("DECRYPT_FAILED", "the decrypted value is not text") from None


def reencrypt(envelope: str, *, old: DataKey, new: DataKey, aad: bytes) -> str:
    """Data-key rotation: decrypt with ``old`` and seal again with ``new`` under the same associated data."""
    return encrypt(new, decrypt(old, envelope, aad), aad)


def envelope_key_id(envelope: str) -> str:
    """The key id named in a ciphertext or wrapped key, to pick the key that opens it."""
    return _parse(envelope)[0]


def derive_fingerprint_key(master_key: bytes, company_id: int) -> bytes:
    """Per-tenant fingerprint key: HKDF-SHA256 of the master key with the company id as context."""
    _check_key(master_key, "master key")
    if not isinstance(company_id, int) or isinstance(company_id, bool) or company_id <= 0:
        raise CryptoError("INVALID_ARGUMENT", "company_id must be a positive integer")
    info = f"{FINGERPRINT_VERSION}|company={company_id}".encode("ascii")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=_FINGERPRINT_SALT, info=info).derive(master_key)


def _identity_part(value: str, label: str, *, required: bool) -> str:
    if not isinstance(value, str):
        raise CryptoError("INVALID_IDENTITY", f"the {label} must be text")
    normalised = "".join(value.split()).replace("-", "").upper()
    if _IDENTITY_PART.fullmatch(normalised) is None or (required and not normalised):
        raise CryptoError("INVALID_IDENTITY", f"the {label} must be letters and digits")
    return normalised


def fingerprint(fp_key: bytes, country: str, routing: str, account: str) -> str:
    """Hex HMAC-SHA256 of the normalised ``country|routing|account`` (spaces, hyphens and case ignored)."""
    _check_key(fp_key, "fingerprint key")
    country_code = _identity_part(country, "country", required=True)
    if _COUNTRY.fullmatch(country_code) is None:
        raise CryptoError("INVALID_IDENTITY", "the country must be a two-letter code")
    message = "|".join(
        (
            FINGERPRINT_VERSION,
            country_code,
            _identity_part(routing, "routing code", required=False),
            _identity_part(account, "account", required=True),
        )
    )
    return hmac.new(fp_key, message.encode("ascii"), hashlib.sha256).hexdigest()


def fingerprints_equal(a: str, b: str) -> bool:
    """Constant-time comparison of two fingerprints."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

"""Envelope encryption, fingerprints (3.7 §12, 3.9 §9, decision I-10) and masking (EC-BNK-11, EC-PAY-21)."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from app.payroll.banking import crypto
from app.payroll.banking.crypto import (
    DataKey,
    decrypt,
    derive_fingerprint_key,
    encrypt,
    envelope_key_id,
    fingerprint,
    fingerprints_equal,
    generate_data_key,
    load_master_key,
    reencrypt,
    rewrap,
    unwrap_key,
    wrap_key,
)
from app.payroll.banking.errors import CryptoError
from app.payroll.banking.masking import (
    Sensitive,
    last4,
    mask_account,
    mask_iban,
    mask_name,
    redact,
    reveals,
    scrub_text,
)

MASTER = bytes(range(32))
MASTER_2 = bytes(range(1, 33))
AAD = b"bank_accounts.account_number:7:42"
ENVELOPE = re.compile(r"v1:[A-Za-z0-9_.-]+:[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+")


def _dek() -> DataKey:
    return DataKey("dk-tenant7", bytes(range(100, 132)))


# --------------------------------------------------------------------------- encryption


def test_encrypt_round_trip_and_envelope_format() -> None:
    dek = _dek()
    sealed = encrypt(dek, "123456784321", AAD)
    assert ENVELOPE.fullmatch(sealed)
    version, key_id, nonce, body = sealed.split(":")
    assert (version, key_id) == ("v1", "dk-tenant7")
    assert len(base64.b64decode(nonce)) == 12
    assert len(base64.b64decode(body)) == len("123456784321") + 16
    assert "123456784321" not in sealed
    assert decrypt(dek, sealed, AAD) == "123456784321"
    assert envelope_key_id(sealed) == "dk-tenant7"


def test_nonces_are_fresh_per_encryption() -> None:
    dek = _dek()
    assert encrypt(dek, "same", AAD) != encrypt(dek, "same", AAD)


def test_aad_binding_prevents_moving_ciphertext_between_rows() -> None:
    dek = _dek()
    sealed = encrypt(dek, "AE070331234567890123456", AAD)
    with pytest.raises(CryptoError) as exc:
        decrypt(dek, sealed, b"bank_accounts.account_number:8:42")
    assert exc.value.code == "DECRYPT_FAILED"


def test_tampering_and_wrong_key_fail_authentication() -> None:
    dek = _dek()
    sealed = encrypt(dek, "123456784321", AAD)
    prefix, body = sealed.rsplit(":", 1)
    raw = bytearray(base64.b64decode(body))
    raw[0] ^= 1
    with pytest.raises(CryptoError) as tampered:
        decrypt(dek, f"{prefix}:{base64.b64encode(bytes(raw)).decode()}", AAD)
    assert tampered.value.code == "DECRYPT_FAILED"
    other = DataKey("dk-tenant7", bytes(32))
    with pytest.raises(CryptoError) as wrong:
        decrypt(other, sealed, AAD)
    assert wrong.value.code == "DECRYPT_FAILED"
    with pytest.raises(CryptoError) as mismatch:
        decrypt(DataKey("dk-other", dek.material), sealed, AAD)
    assert mismatch.value.code == "KEY_ID_MISMATCH"


def test_header_rewrite_to_another_key_id_fails() -> None:
    dek = _dek()
    sealed = encrypt(dek, "123456784321", AAD)
    forged = sealed.replace("dk-tenant7", "dk-forged", 1)
    with pytest.raises(CryptoError) as exc:
        decrypt(DataKey("dk-forged", dek.material), forged, AAD)
    assert exc.value.code == "DECRYPT_FAILED"


@pytest.mark.parametrize(
    "envelope,code",
    [
        ("v2:k:AAAA:AAAA", "UNSUPPORTED_VERSION"),
        ("v1:k:AAAA", "MALFORMED_ENVELOPE"),
        ("v1:k:not base64!:AAAA", "MALFORMED_ENVELOPE"),
        ("v1:k:AAAA:AAAA", "MALFORMED_ENVELOPE"),
        ("v1:bad key:AAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAA==", "INVALID_KEY_ID"),
    ],
)
def test_malformed_envelopes_raise_typed_errors(envelope: str, code: str) -> None:
    with pytest.raises(CryptoError) as exc:
        decrypt(_dek(), envelope, AAD)
    assert exc.value.code == code


def test_key_wrap_unwrap_and_master_rotation() -> None:
    dek = generate_data_key("dk-1")
    wrapped = wrap_key(MASTER, dek, "mk-2026")
    assert envelope_key_id(wrapped) == "mk-2026"
    assert unwrap_key(MASTER, wrapped, "dk-1").material == dek.material
    with pytest.raises(CryptoError):
        unwrap_key(MASTER, wrapped, "dk-2")
    with pytest.raises(CryptoError):
        unwrap_key(MASTER_2, wrapped, "dk-1")
    rotated = rewrap(wrapped, dek_key_id="dk-1", old_master_key=MASTER, new_master_key=MASTER_2, new_key_id="mk-2027")
    assert envelope_key_id(rotated) == "mk-2027"
    restored = unwrap_key(MASTER_2, rotated, "dk-1")
    sealed = encrypt(dek, "50100123456789", AAD)
    assert decrypt(restored, sealed, AAD) == "50100123456789"


def test_data_key_rotation_reencrypts() -> None:
    old, new = generate_data_key("dk-old"), generate_data_key("dk-new")
    sealed = encrypt(old, "GB82WEST12345698765432", AAD)
    moved = reencrypt(sealed, old=old, new=new, aad=AAD)
    assert envelope_key_id(moved) == "dk-new"
    assert decrypt(new, moved, AAD) == "GB82WEST12345698765432"


def test_generated_keys_are_256_bit_and_never_in_repr() -> None:
    dek = generate_data_key()
    assert len(dek.material) == 32 and dek.key_id.startswith("dk-")
    assert dek.material.hex() not in repr(dek)
    assert repr(dek) == f"DataKey(key_id={dek.key_id!r})"


def test_invalid_keys_rejected() -> None:
    with pytest.raises(CryptoError) as exc:
        DataKey("dk", b"short")
    assert exc.value.code == "INVALID_KEY"
    with pytest.raises(CryptoError):
        wrap_key(b"x" * 16, _dek(), "mk")
    with pytest.raises(CryptoError):
        encrypt(_dek(), b"bytes", AAD)  # type: ignore[arg-type]
    assert load_master_key(base64.b64encode(MASTER).decode()) == MASTER
    with pytest.raises(CryptoError):
        load_master_key(base64.b64encode(b"x" * 16).decode())


def test_error_messages_never_contain_plaintext_or_keys() -> None:
    dek = _dek()
    sealed = encrypt(dek, "123456784321", AAD)
    try:
        decrypt(DataKey("dk-tenant7", MASTER), sealed, AAD)
    except CryptoError as exc:
        text = f"{exc} {exc!r} {exc.message}"
        assert "123456784321" not in text
        assert MASTER.hex() not in text and sealed not in text


# --------------------------------------------------------------------------- fingerprints


def test_fingerprint_is_hmac_of_normalised_identity() -> None:
    key = derive_fingerprint_key(MASTER, 7)
    a = fingerprint(key, "IN", "HDFC", "5010 0123-4567 89")
    b = fingerprint(key, "in", "hdfc", "50100123456789")
    assert a == b and re.fullmatch(r"[0-9a-f]{64}", a)
    assert fingerprints_equal(a, b)
    assert not fingerprints_equal(a, fingerprint(key, "IN", "HDFC", "50100123456788"))


def test_ec_bnk_07_fingerprint_detects_reuse_within_tenant_only() -> None:
    tenant_a, tenant_b = derive_fingerprint_key(MASTER, 1), derive_fingerprint_key(MASTER, 2)
    assert tenant_a != tenant_b
    same_a = fingerprint(tenant_a, "AE", "", "AE070331234567890123456")
    assert same_a == fingerprint(tenant_a, "AE", "", "ae07 0331 2345 6789 0123 456")
    assert same_a != fingerprint(tenant_b, "AE", "", "AE070331234567890123456")


def test_fingerprint_rejects_bad_identity() -> None:
    key = derive_fingerprint_key(MASTER, 7)
    with pytest.raises(CryptoError):
        fingerprint(key, "IND", "HDFC", "1")
    with pytest.raises(CryptoError):
        fingerprint(key, "IN", "HDFC", "")
    with pytest.raises(CryptoError):
        derive_fingerprint_key(MASTER, 0)


def test_crypto_module_is_pure() -> None:
    source = Path(crypto.__file__).read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "fastapi", "app.database", "app.models", "app.config", "os.environ", "datetime.now"):
        assert forbidden not in source


# --------------------------------------------------------------------------- masking (EC-BNK-11, EC-PAY-21)


def test_ec_bnk_11_mask_account_shows_last_four() -> None:
    assert mask_account("123456784321") == "••••4321"
    assert mask_account("1234 5678 4321") == "••••4321"
    assert last4("123456784321") == "4321"


def test_ec_bnk_11_short_numbers_hide_at_least_four() -> None:
    assert mask_account("123456") == "••••56"
    assert mask_account("1234") == "••••"
    assert mask_account(None) == "" and mask_account("  ") == ""


def test_mask_iban_and_name() -> None:
    assert mask_iban("AE07 0331 2345 6789 0123 456") == "AE••••3456"
    assert mask_name("Asha Rao") == "A••• R•••"
    assert mask_name(None) == ""


def test_ec_bnk_11_redact_nested_payloads() -> None:
    payload = {
        "employee_id": 7,
        "Account_Number": "123456784321",
        "beneficiary": {"iban": "AE070331234567890123456", "name": "Asha"},
        "history": [{"account": "50100123456789"}],
        "account_number_enc": b"\x00\x01",
    }
    out = redact(payload)
    assert out["Account_Number"] == "••••4321"
    assert out["beneficiary"] == {"iban": "AE••••3456", "name": "Asha"}
    assert out["history"] == [{"account": "••••6789"}]
    assert out["account_number_enc"] == "[encrypted]"
    assert out["employee_id"] == 7


def test_ec_bnk_11_scrub_free_text() -> None:
    text = "Transfer to AE070331234567890123456 failed for account 50100123456789 on 2026-10-20"
    scrubbed = scrub_text(text)
    assert "AE070331234567890123456" not in scrubbed and "50100123456789" not in scrubbed
    assert "AE••••3456" in scrubbed and "••••6789" in scrubbed and "2026-10-20" in scrubbed


def test_ec_pay_21_notification_never_contains_full_bank_details() -> None:
    account = "50100123456789"
    notification = f"Your salary was sent to HDFC {mask_account(account)}."
    assert not reveals(notification, [account, "AE070331234567890123456"])
    assert reveals("sent to 5010 0123 4567 89", [account])


def test_sensitive_wrapper_masks_repr_str_and_format() -> None:
    value = Sensitive("123456784321")
    assert repr(value) == "Sensitive('••••4321')"
    assert str(value) == "••••4321" and f"{value}" == "••••4321"
    assert value.reveal() == "123456784321"

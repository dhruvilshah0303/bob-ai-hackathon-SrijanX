"""Unit tests for auth_service.py: password hashing and JWT issue/verify,
independent of any HTTP layer (no TestClient, no DB required)."""
import time
from datetime import timedelta

import jwt
import pytest

import auth_service
import config
from models import Role


class _FakeUser:
    def __init__(self, id="u1", role=Role.DISPATCHER, hospital_id=None):
        self.id = id
        self.role = role
        self.hospital_id = hospital_id


def test_hash_password_is_not_plaintext_and_verifies():
    hashed = auth_service.hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert auth_service.verify_password("correct horse battery staple", hashed)


def test_verify_password_rejects_wrong_password():
    hashed = auth_service.hash_password("correct horse battery staple")
    assert not auth_service.verify_password("wrong password", hashed)


def test_verify_password_never_raises_on_garbage_hash():
    # A corrupted/legacy hash should fail closed, not 500.
    assert not auth_service.verify_password("anything", "not-a-real-bcrypt-hash")


def test_access_token_round_trips_claims():
    user = _FakeUser(id="u-42", role=Role.HOSPITAL_ADMIN, hospital_id="h-7")
    token = auth_service.create_access_token(user)
    payload = auth_service.decode_token(token, expected_type="access")
    assert payload["sub"] == "u-42"
    assert payload["role"] == Role.HOSPITAL_ADMIN
    assert payload["hospital_id"] == "h-7"
    assert payload["type"] == "access"


def test_refresh_token_rejected_as_access_token():
    user = _FakeUser()
    refresh = auth_service.create_refresh_token(user)
    with pytest.raises(auth_service.TokenError):
        auth_service.decode_token(refresh, expected_type="access")


def test_access_token_rejected_as_refresh_token():
    user = _FakeUser()
    access = auth_service.create_access_token(user)
    with pytest.raises(auth_service.TokenError):
        auth_service.decode_token(access, expected_type="refresh")


def test_expired_token_is_rejected():
    user = _FakeUser()
    expired = auth_service._create_token(user, "access", timedelta(seconds=-1))
    with pytest.raises(auth_service.TokenError):
        auth_service.decode_token(expired, expected_type="access")


def test_token_signed_with_wrong_secret_is_rejected():
    forged = jwt.encode(
        {"sub": "attacker", "role": Role.ADMIN, "hospital_id": None, "type": "access"},
        "not-the-real-secret",
        algorithm=config.JWT_ALGORITHM,
    )
    with pytest.raises(auth_service.TokenError):
        auth_service.decode_token(forged, expected_type="access")


def test_tampered_role_claim_is_rejected():
    """A client can't forge an elevated role by hand-editing a JWT payload -
    changing any byte invalidates the HMAC signature."""
    user = _FakeUser(role=Role.DISPATCHER)
    token = auth_service.create_access_token(user)
    header_b64, payload_b64, sig_b64 = token.split(".")
    tampered = header_b64 + "." + payload_b64.replace("D", "A", 1) + "." + sig_b64
    with pytest.raises(auth_service.TokenError):
        auth_service.decode_token(tampered, expected_type="access")

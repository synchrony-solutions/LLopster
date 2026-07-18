"""Tests for the license-key framework (ROADMAP #8).

No live anything — each test mints its own ephemeral Ed25519 keypair, signs
fixtures inline, and verifies against that key. The production embedded key is
never needed (and the private half doesn't exist in CI).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agent import entitlements
from src.agent import license as lic


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def keys() -> tuple[bytes, str]:
    """An ephemeral Ed25519 keypair as (private_pem_bytes, public_pem_str)."""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem


def _sign(priv_pem: bytes, *, tier="business", features=None, days=365, **extra) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "tier": tier,
        "features": features or [],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=days)).timestamp()),
    }
    claims.update(extra)
    return jwt.encode(claims, priv_pem, algorithm="EdDSA")


@pytest.fixture(autouse=True)
def _clear_override():
    """Ensure no override leaks between tests."""
    lic.set_active_license_key(None)
    yield
    lic.set_active_license_key(None)


# ---------------------------------------------------------------------------
# verify_license — pure function
# ---------------------------------------------------------------------------

def test_valid_business_license_enables_its_features(keys):
    priv, pub = keys
    token = _sign(priv, features=["multi_cluster", "pack:jvm-pack"], license_id="acme-1")
    result = lic.verify_license(token, public_key=pub)

    assert result.valid is True
    assert result.source == "license"
    assert result.tier == "business"
    assert result.license_id == "acme-1"
    assert result.is_enabled("multi_cluster") is True
    assert result.is_enabled("pack:jvm-pack") is True
    assert result.is_enabled("sso") is False  # not granted
    assert result.is_enabled("") is False


def test_missing_key_is_embedded_community(keys):
    for raw in (None, "", "   "):
        result = lic.verify_license(raw)
        assert result.tier == lic.TIER_COMMUNITY
        assert result.source == "embedded"
        assert result.valid is True
        assert result.is_enabled("multi_cluster") is False


def test_expired_license_falls_back_to_community(keys):
    priv, pub = keys
    token = _sign(priv, features=["multi_cluster"], days=-1)  # already expired
    result = lic.verify_license(token, public_key=pub)

    assert result.tier == lic.TIER_COMMUNITY
    assert result.source == "invalid"
    assert result.reason == "expired"
    assert result.is_enabled("multi_cluster") is False


def test_bad_signature_falls_back_to_community(keys):
    priv, _pub = keys
    token = _sign(priv, features=["multi_cluster"])
    # Verify against a DIFFERENT key → signature mismatch.
    other_pub = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    result = lic.verify_license(token, public_key=other_pub)

    assert result.tier == lic.TIER_COMMUNITY
    assert result.source == "invalid"
    assert result.reason and result.reason.startswith("invalid")
    assert result.is_enabled("multi_cluster") is False


def test_malformed_token_falls_back_to_community():
    result = lic.verify_license("not-a-jwt-at-all")
    assert result.tier == lic.TIER_COMMUNITY
    assert result.source == "invalid"


def test_token_without_exp_is_rejected(keys):
    priv, pub = keys
    # Hand-build a token missing the required exp claim.
    token = jwt.encode({"tier": "business", "features": ["x"]}, priv, algorithm="EdDSA")
    result = lic.verify_license(token, public_key=pub)
    assert result.source == "invalid"
    assert result.is_enabled("x") is False


def test_clusters_and_expiry_parsed(keys):
    priv, pub = keys
    token = _sign(priv, features=[], clusters=5, license_id="acme-2")
    result = lic.verify_license(token, public_key=pub)
    assert result.clusters == 5
    assert isinstance(result.expires_at, datetime)


# ---------------------------------------------------------------------------
# Active license — override → env → community precedence
# ---------------------------------------------------------------------------

def test_active_license_reads_env(keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(lic, "PUBLIC_KEY_PEM", pub)
    token = _sign(priv, features=["multi_cluster"])
    monkeypatch.setenv(lic.LICENSE_KEY_ENV, token)

    assert lic.is_feature_enabled("multi_cluster") is True
    assert lic.is_feature_enabled("sso") is False


def test_no_env_is_community(monkeypatch):
    monkeypatch.delenv(lic.LICENSE_KEY_ENV, raising=False)
    assert lic.get_active_license().tier == lic.TIER_COMMUNITY
    assert lic.is_feature_enabled("multi_cluster") is False


def test_override_takes_precedence_over_env(keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(lic, "PUBLIC_KEY_PEM", pub)
    env_token = _sign(priv, features=["multi_cluster"])
    override_token = _sign(priv, features=["sso"])
    monkeypatch.setenv(lic.LICENSE_KEY_ENV, env_token)

    lic.set_active_license_key(override_token)
    assert lic.is_feature_enabled("sso") is True
    assert lic.is_feature_enabled("multi_cluster") is False  # override wins

    # Clearing the override falls back to the env key.
    lic.set_active_license_key(None)
    assert lic.is_feature_enabled("multi_cluster") is True


# ---------------------------------------------------------------------------
# entitlements routing — license first, env allow-list fallback
# ---------------------------------------------------------------------------

def test_is_pack_enabled_via_license(keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(lic, "PUBLIC_KEY_PEM", pub)
    token = _sign(priv, features=["pack:jvm-pack"])
    monkeypatch.setenv(lic.LICENSE_KEY_ENV, token)
    monkeypatch.delenv(entitlements.PACK_TOKEN_ENV, raising=False)

    assert entitlements.is_pack_enabled("jvm-pack") is True
    assert entitlements.is_pack_enabled("postgres-pack") is False
    assert entitlements.is_pack_enabled("") is False


def test_is_pack_enabled_env_fallback_without_license(monkeypatch):
    # No license key at all — the interim allow-list must still work.
    monkeypatch.delenv(lic.LICENSE_KEY_ENV, raising=False)
    monkeypatch.setenv(entitlements.PACK_TOKEN_ENV, "jvm-pack, example-pack")

    assert entitlements.is_pack_enabled("jvm-pack") is True
    assert entitlements.is_pack_enabled("example-pack") is True
    assert entitlements.is_pack_enabled("not-entitled") is False


def test_is_pack_enabled_no_license_no_token(monkeypatch):
    monkeypatch.delenv(lic.LICENSE_KEY_ENV, raising=False)
    monkeypatch.delenv(entitlements.PACK_TOKEN_ENV, raising=False)
    assert entitlements.is_pack_enabled("jvm-pack") is False

"""License-key framework — the single gate for paid-feature entitlement.

LLopster ships **one image and one chart for every tier** Paid *code* features live, dormant, in the open Community image and switch
on at runtime when a signed license key grants them; paid *content* (premium
packs) is loaded from outside the repo and gated through this same module (see
``src/agent/entitlements.py``). The rest of the codebase asks exactly one
question — ``is_feature_enabled("multi_cluster")`` — and never touches the JWT
itself.

Design
------
- **Signed JWT, asymmetric (Ed25519 / EdDSA).** The *public* verification key is
  embedded below; the *private* signing key never ships in the image (it lives in
  a secret manager — see ``scripts/sign_license.py``). A competitor can read this
  public key but cannot mint a license with it.
- **Offline-first.** Verification is purely local — signature + ``exp`` check, no
  network call. (Usage telemetry reporting back to a license server is a Phase 3
  concern and is deliberately NOT built here.)
- **Fail-safe to Community, never crash.** A missing, malformed, badly-signed, or
  expired key all resolve to the embedded Community license, which grants no paid
  features. This mirrors LLopster's graceful-degradation contract: a missing
  credential disables a feature, it never raises.

Source of the active key (precedence, highest first)
----------------------------------------------------
1. An explicit override set by ``set_active_license_key()`` — used by the agent to
   apply the runtime ``license_key`` Setting (UI-pasted key, no redeploy).
2. The ``LLOPSTER_LICENSE_KEY`` env var — the deploy-time source of truth, mounted
   from a Secret in the chart (same pattern as ``ANTHROPIC_API_KEY``).
3. Nothing → the embedded Community license.

This mirrors the repository-wide "Setting-table override → env default" pattern
used by ``run_retention_days``, ``triage_enabled``, etc.

This module imports nothing from other agent modules so it can be a leaf
dependency of ``entitlements`` (which routes through it) without a cycle.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import jwt

log = logging.getLogger("llopster.license")

# Env var carrying the deploy-time license key. Mounted from a Secret in the
# chart; absent in every Community deployment.
LICENSE_KEY_ENV = "LLOPSTER_LICENSE_KEY"

# JWT signature algorithm. Ed25519 → short keys, fast verification.
_ALGORITHM = "EdDSA"

# Embedded public verification key. Safe to ship — it can only *verify* a
# license, never *mint* one. The matching private key is held outside this
# repo (secret manager / ``.license-signing-key.pem`` in dev).
PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAwWefjnvIxmIJRnPZxeZYz5MbyFYIOl0+jHroBECZ4p4=\n"
    "-----END PUBLIC KEY-----\n"
)

# Canonical tier names. ``tier`` is informational/display today; gating is done
# per-feature (the JWT's ``features`` claim), so a license can grant any subset.
TIER_COMMUNITY = "community"
TIER_BUSINESS = "business"
TIER_ENTERPRISE = "enterprise"


@dataclass(frozen=True)
class License:
    """A resolved, verified license. Always a valid object — failures resolve to
    a Community license with ``source`` describing why."""

    tier: str
    features: frozenset[str] = field(default_factory=frozenset)
    valid: bool = True
    # "embedded" (no key → Community), "license" (verified JWT), or
    # "invalid" (a key was present but failed verification → Community).
    source: str = "embedded"
    license_id: str | None = None
    expires_at: datetime | None = None
    clusters: int | None = None
    # Human-readable note for the dashboard when a key was rejected.
    reason: str | None = None

    def is_enabled(self, feature: str) -> bool:
        """True iff this license grants ``feature``. Community licenses grant
        nothing paid, so this is False for every paid feature on Community."""
        if not feature:
            return False
        return feature in self.features


def _community(*, source: str = "embedded", reason: str | None = None) -> License:
    """The fallback Community license: valid deployment, zero paid features."""
    return License(
        tier=TIER_COMMUNITY,
        features=frozenset(),
        valid=True,
        source=source,
        reason=reason,
    )


def verify_license(raw_jwt: str | None, *, public_key: str | None = None) -> License:
    """Verify a raw license JWT and return a :class:`License`.

    Pure function — no env, no globals beyond the embedded key, no I/O. Any
    failure (missing / malformed / bad signature / expired / unknown shape)
    resolves to a Community license rather than raising, so callers can treat
    the result uniformly.
    """
    raw = (raw_jwt or "").strip()
    if not raw:
        return _community()

    pk = public_key or PUBLIC_KEY_PEM
    try:
        claims = jwt.decode(
            raw,
            pk,
            algorithms=[_ALGORITHM],
            options={"require": ["exp"]},
        )
    except jwt.ExpiredSignatureError:
        log.warning("license: key has expired — falling back to Community tier")
        return _community(source="invalid", reason="expired")
    except jwt.InvalidTokenError as e:  # bad signature, malformed, missing exp, ...
        log.warning("license: invalid key (%s) — falling back to Community tier", e)
        return _community(source="invalid", reason=f"invalid: {e}")

    # Defensive parse: a verified-but-oddly-shaped token still degrades to
    # Community rather than handing the caller a half-built License.
    features_raw = claims.get("features", [])
    if not isinstance(features_raw, (list, tuple)):
        log.warning("license: 'features' claim is not a list — ignoring it")
        features_raw = []
    features = frozenset(str(f) for f in features_raw if f)

    expires_at: datetime | None = None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)

    clusters = claims.get("clusters")
    if not isinstance(clusters, int):
        clusters = None

    lic = License(
        tier=str(claims.get("tier", "unknown")),
        features=features,
        valid=True,
        source="license",
        license_id=claims.get("license_id") or claims.get("sub"),
        expires_at=expires_at,
        clusters=clusters,
    )
    log.info(
        "license: verified tier=%s id=%s features=%d expires=%s",
        lic.tier, lic.license_id, len(lic.features),
        lic.expires_at.isoformat() if lic.expires_at else "never",
    )
    return lic


# ---------------------------------------------------------------------------
# Active license — the singleton the rest of the app queries.
#
# We intentionally do NOT cache the parsed License: verification re-reads the
# effective key (override → env) on each call so monkeypatched env in tests and
# UI-applied overrides take effect immediately, and so an ``exp`` that lapses
# while the process runs is honored on the next check. Ed25519 verification is
# microseconds, and the prompt path only calls this per alert — not hot.
# ---------------------------------------------------------------------------

_override_key: str | None = None


def set_active_license_key(raw_key: str | None) -> None:
    """Override the active license key (highest precedence).

    The agent calls this on startup (and after a settings change) with the
    ``license_key`` value from the Setting table. ``None`` clears the override
    and falls back to the env var.
    """
    global _override_key
    _override_key = raw_key


def _effective_raw_key() -> str:
    if _override_key is not None:
        return _override_key
    return os.getenv(LICENSE_KEY_ENV, "")


def get_active_license() -> License:
    """Resolve and verify the active license (override → env → Community)."""
    return verify_license(_effective_raw_key())


def get_license_status() -> License:
    """Alias for :func:`get_active_license`, named for dashboard/diagnostic use."""
    return get_active_license()


def is_feature_enabled(feature: str, license: License | None = None) -> bool:
    """The one call the rest of the codebase makes to gate a paid feature.

    ``license`` defaults to the active license; pass one explicitly in tests.
    """
    lic = license if license is not None else get_active_license()
    return lic.is_enabled(feature)

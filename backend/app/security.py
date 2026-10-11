"""Password hashing, JWTs, API tokens and installer callback tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as pysecrets
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import settings

TOKEN_PREFIX = "doz_"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Session JWTs
# ---------------------------------------------------------------------------


def create_access_token(customer_id: uuid.UUID, is_admin: bool) -> tuple[str, int]:
    now = datetime.now(UTC)
    expires_in = settings.access_token_ttl_seconds
    payload = {
        "sub": str(customer_id),
        "adm": is_admin,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def create_kvm_ticket(customer_id: uuid.UUID, hours: int) -> str:
    """What the browser carries to the KVM proxy: who it is, until when."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(customer_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=hours)).timestamp()),
        "typ": "kvm",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_kvm_ticket(token: str) -> dict:
    payload = jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub"]},
    )
    if payload.get("typ") != "kvm":
        raise jwt.InvalidTokenError("not a KVM ticket")
    return payload


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub"]},
    )


# ---------------------------------------------------------------------------
# API tokens
#
# Shown once at creation, stored only as a hash. SHA-256 rather than bcrypt:
# these are 256-bit random values, so there is nothing to brute-force, and API
# calls cannot afford a bcrypt round trip on every request.
# ---------------------------------------------------------------------------


def generate_api_token() -> tuple[str, str, str]:
    """Return `(token, prefix, hash)`."""
    raw = pysecrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{raw}"
    return token, token[: len(TOKEN_PREFIX) + 8], hash_api_token(token)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_prefix(token: str) -> str:
    return token[: len(TOKEN_PREFIX) + 8]


# ---------------------------------------------------------------------------
# Installer callback tokens
#
# The ramdisk is handed one of these over the provisioning VLAN. It authorises
# exactly one job: progress updates and a single completion callback.
# ---------------------------------------------------------------------------


def generate_callback_token() -> tuple[str, str]:
    token = pysecrets.token_urlsafe(32)
    return token, hash_api_token(token)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------
# Boot script authentication
#
# iPXE cannot present a bearer token, so the per-MAC boot URL carries an HMAC
# derived from the MAC. This is not a substitute for keeping the provisioning
# VLAN closed; it stops a machine on that VLAN from fetching another host's
# boot script and its embedded credentials.
# ---------------------------------------------------------------------------


def boot_signature(mac: str) -> str:
    digest = hmac.new(settings.jwt_secret.encode(), mac.lower().encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)[:22].decode()


def verify_boot_signature(mac: str, signature: str) -> bool:
    return hmac.compare_digest(boot_signature(mac), signature or "")


def normalise_mac(mac: str) -> str:
    """Accept `aa:bb:...`, `aa-bb-...` or bare hex; emit colon-separated lower."""
    cleaned = "".join(c for c in mac.lower() if c in "0123456789abcdef")
    if len(cleaned) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))

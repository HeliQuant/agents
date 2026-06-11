"""HeliQuant — Tier 2 multi-tenancy scaffold (see docs/MULTI-TENANCY.md §2).

Mints the per-tenant Supabase JWT a self-hosted instance uses INSTEAD of the service_role key, so
every tenant writes only its own rows (enforced by the RLS in deploy/multitenant_schema.sql). The
JWT carries {role: "authenticated", tenant_id} and is signed with the project's JWT secret — which
lives ONLY where this runs (a token endpoint), never on the user side.

SCAFFOLD — deliberately NOT imported by app.py / the live cycle, so the running single-tenant demo
is untouched. Wiring plan in the module docstring of state_store + docs/MULTI-TENANCY.md §4.

Zero new dependencies: HS256 is implemented over stdlib hmac/hashlib/base64.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

DEMO_TENANT = "00000000-0000-0000-0000-000000000000"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_tenant_jwt(tenant_id: str, ttl_hours: int = 24, secret: str | None = None) -> str:
    """Sign a Supabase-compatible HS256 JWT scoping the caller to `tenant_id`. Hand the RESULT to a
    user as their SUPABASE_KEY; the RLS policy `tenant_id = (auth.jwt()->>'tenant_id')::uuid` then
    pins every write they make to their own rows. Never hand out the service_role key."""
    secret = secret or os.environ.get("SUPABASE_JWT_SECRET")
    if not secret:
        raise RuntimeError("SUPABASE_JWT_SECRET not set (the project's JWT secret — token endpoint only)")
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"role": "authenticated", "tenant_id": tenant_id, "iat": now, "exp": now + ttl_hours * 3600}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def token_hash(opaque_token: str) -> str:
    """For the write-proxy path (§2b): store only the hash of an opaque tenant token, never the token."""
    return hashlib.sha256(opaque_token.encode()).hexdigest()


def current_tenant() -> str:
    """The tenant this instance acts as. Self-hosters set HQ_TENANT_ID; the demo runs as DEMO_TENANT."""
    return os.environ.get("HQ_TENANT_ID", DEMO_TENANT)


# Wiring TODO (kept out of the live path on purpose):
#   state_store.save/load gain an optional tenant_id (default current_tenant()); the Supabase client
#   is created with the tenant JWT instead of service_role; reads stay public. One-file change once
#   the schema migration is applied — see docs/MULTI-TENANCY.md §4 steps 3-4.

if __name__ == "__main__":
    os.environ.setdefault("SUPABASE_JWT_SECRET", "dev-only-secret-not-real")
    jwt = mint_tenant_jwt("11111111-2222-3333-4444-555555555555", ttl_hours=12)
    head_b64, payload_b64, _ = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    print("tenant JWT:", jwt[:48], "...")
    print("header :", base64.urlsafe_b64decode(pad(head_b64)).decode())
    print("payload:", base64.urlsafe_b64decode(pad(payload_b64)).decode())
    print("current_tenant():", current_tenant())

"""
API-key authentication for the crypto-market-data MCP server.

Clients authenticate over HTTP with `Authorization: Bearer <api-key>`. Each key
maps to a client_id and a pricing tier. Tiers drive rate limits / quotas
(see ratelimit.py).

Keys are loaded from (in order):
    1. CRYPTO_MCP_API_KEYS_FILE  -> JSON file {token: {client_id, tier}} or {token: tier}
    2. CRYPTO_MCP_API_KEYS       -> "token:client_id:tier,token2::pro" (client_id/tier optional)

If no keys are configured in HTTP mode, a throwaway DEMO key (tier=pro) is
generated and printed to stderr so you can test immediately.
"""

from __future__ import annotations

import json
import logging
import os
import secrets

from fastmcp.server.auth import AccessToken, TokenVerifier

log = logging.getLogger("crypto-mcp.auth")

# Pricing tiers — wire these to your billing (MCPize plan, Stripe price, etc.).
TIERS: dict[str, dict] = {
    "free":  {"rpm": 10,  "monthly_quota": 1_000},
    "pro":   {"rpm": 120, "monthly_quota": 100_000},
    "ultra": {"rpm": 600, "monthly_quota": 2_000_000},
}
DEFAULT_TIER = "free"


def load_keys() -> dict[str, dict]:
    """Load API keys into {token: {"client_id": str, "tier": str}}."""
    store: dict[str, dict] = {}

    path = os.environ.get("CRYPTO_MCP_API_KEYS_FILE")
    if path and os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
        for token, v in raw.items():
            if isinstance(v, str):
                store[token] = {"client_id": token[:8], "tier": v}
            else:
                store[token] = {
                    "client_id": v.get("client_id", token[:8]),
                    "tier": v.get("tier", DEFAULT_TIER),
                }

    env = os.environ.get("CRYPTO_MCP_API_KEYS")
    if env:
        for entry in env.split(","):
            parts = [p.strip() for p in entry.strip().split(":")]
            if not parts or not parts[0]:
                continue
            token = parts[0]
            client_id = parts[1] if len(parts) > 1 and parts[1] else token[:8]
            tier = parts[2] if len(parts) > 2 and parts[2] else DEFAULT_TIER
            store[token] = {"client_id": client_id, "tier": tier}

    # validate tiers
    for token, rec in store.items():
        if rec["tier"] not in TIERS:
            log.warning("Key %s… has unknown tier '%s'; falling back to '%s'.",
                        token[:6], rec["tier"], DEFAULT_TIER)
            rec["tier"] = DEFAULT_TIER

    return store


class APIKeyVerifier(TokenVerifier):
    """Verifies static API keys (Bearer tokens) against the configured store."""

    def __init__(self, keys: dict[str, dict] | None = None):
        super().__init__()
        self.keys = keys if keys is not None else load_keys()
        if not self.keys:
            demo = "demo-" + secrets.token_urlsafe(24)
            self.keys[demo] = {"client_id": "demo", "tier": "pro"}
            log.warning(
                "No API keys configured. Generated DEMO key (tier=pro):\n"
                "    %s\n"
                "Send it as 'Authorization: Bearer <key>'. "
                "Set CRYPTO_MCP_API_KEYS to manage real keys.",
                demo,
            )
        log.info("Loaded %d API key(s).", len(self.keys))

    async def verify_token(self, token: str) -> AccessToken | None:
        rec = self.keys.get(token)
        if not rec:
            return None
        tier = rec.get("tier", DEFAULT_TIER)
        return AccessToken(
            token=token,
            client_id=rec["client_id"],
            scopes=[tier],
            claims={"tier": tier, "client_id": rec["client_id"]},
        )

"""
Per-client rate limiting + monthly quota for the crypto-market-data MCP server.

Enforced as FastMCP middleware on every tool call. Limits come from the
authenticated client's tier (see auth.TIERS):
    - requests-per-minute  (sliding 60s window)
    - monthly call quota   (resets at the UTC month boundary)

State is in-memory (fine for a single instance). For multi-instance / serverless
deployments, back `_calls` / `_month` with Redis or a Durable Object.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from auth import DEFAULT_TIER, TIERS

log = logging.getLogger("crypto-mcp.ratelimit")


class RateLimitMiddleware(Middleware):
    def __init__(self) -> None:
        self._calls: dict[str, deque[float]] = defaultdict(deque)  # client -> recent timestamps
        self._month: dict[str, list] = {}                          # client -> [month_key, count]
        self._lock = asyncio.Lock()

    @staticmethod
    def _identity() -> tuple[str, str]:
        client_id, tier = "anonymous", DEFAULT_TIER
        try:
            tok = get_access_token()
            if tok is not None:
                claims = getattr(tok, "claims", None) or {}
                client_id = claims.get("client_id") or getattr(tok, "client_id", client_id)
                tier = claims.get("tier", DEFAULT_TIER)
        except Exception:  # noqa: BLE001 - no auth context (e.g. stdio) => defaults
            pass
        return client_id, tier

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        client_id, tier = self._identity()
        limits = TIERS.get(tier, TIERS[DEFAULT_TIER])
        now = time.monotonic()
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

        async with self._lock:
            # --- per-minute sliding window ---
            dq = self._calls[client_id]
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) >= limits["rpm"]:
                retry_in = 60 - (now - dq[0])
                log.info("rate-limit hit: client=%s tier=%s", client_id, tier)
                raise ToolError(
                    f"Rate limit exceeded: {limits['rpm']} requests/min for tier "
                    f"'{tier}'. Retry in {retry_in:.0f}s or upgrade your plan."
                )

            # --- monthly quota ---
            m = self._month.get(client_id)
            if not m or m[0] != month_key:
                m = [month_key, 0]
                self._month[client_id] = m
            if m[1] >= limits["monthly_quota"]:
                raise ToolError(
                    f"Monthly quota exceeded: {limits['monthly_quota']} calls for tier "
                    f"'{tier}'. Upgrade your plan to continue."
                )

            # --- record the call ---
            dq.append(now)
            m[1] += 1

        return await call_next(context)

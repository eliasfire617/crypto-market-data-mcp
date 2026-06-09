"""
Crypto Market Data MCP Server
=============================

A read-only Model Context Protocol (MCP) server that gives any AI agent
(Claude, Cursor, Cline, etc.) live cryptocurrency market data across major
exchanges via CCXT. Public endpoints only — no API keys required.

Robustness features:
    - Per-request timeouts + retries with exponential backoff (transient errors)
    - Capability guards (won't call a method an exchange doesn't support; tells
      you which exchanges DO support it)
    - TTL response cache (cuts latency and stays inside exchange rate limits)
    - Structured error objects instead of crashes/exceptions
    - Input validation and clamped limits
    - Concurrent multi-exchange queries
    - Guaranteed exchange cleanup; logs to stderr (safe for stdio transport)

Tools:
    get_price, get_funding_rate, compare_funding, get_funding_rate_history,
    get_open_interest, get_long_short_ratio, get_liquidations, get_order_book,
    get_ohlcv, get_recent_trades, search_symbols, list_exchanges, get_capabilities

Run:
    python server.py            # stdio transport (Claude Desktop / Cursor / Cline)
    python server.py --http     # streamable HTTP on :8000 (hosted / remote)
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Awaitable, Callable

import ccxt.async_support as ccxt
from fastmcp import FastMCP

# --- logging: stderr ONLY (stdout is reserved for the MCP protocol on stdio) ---
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [crypto-mcp] %(message)s",
)
log = logging.getLogger("crypto-mcp")

# --- config ---
SUPPORTED_EXCHANGES = ["bybit", "binance", "okx", "hyperliquid", "gate", "kucoin"]
REQUEST_TIMEOUT_MS = 10_000          # per HTTP request (ccxt)
OVERALL_TIMEOUT_S = 25.0             # hard ceiling per tool call (catches hangs)
MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 0.5
DEFAULT_CACHE_TTL_S = 5.0
MAX_CACHE_ENTRIES = 500

# CCXT errors worth retrying (transient). Everything else is treated as permanent.
RETRYABLE_ERRORS = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
    ccxt.ExchangeNotAvailable,
)

# Auth is enforced on the hosted HTTP transport (where you charge); local stdio
# stays open for single-user dev. Detected here so it can be attached at build time.
HTTP_MODE = "--http" in sys.argv
_auth = None
if HTTP_MODE:
    from auth import APIKeyVerifier

    _auth = APIKeyVerifier()

mcp = FastMCP(
    name="crypto-market-data",
    auth=_auth,
    instructions=(
        "Read-only live crypto market data (price, funding rates, open interest, "
        "long/short ratio, order book, candles, trades) across major exchanges. "
        "Use 'compare_funding' to spot funding-rate arbitrage between exchanges, "
        "'search_symbols' if unsure of the exact symbol, and 'get_capabilities' "
        "to see which exchange supports which data. For perpetuals pass symbols "
        "like 'BTC/USDT:USDT'; for spot use 'BTC/USDT'."
    ),
)

# --------------------------------------------------------------------------- #
# TTL cache
# --------------------------------------------------------------------------- #
_cache: dict[str, tuple[float, float, Any]] = {}  # key -> (stored_at, ttl, value)


def _cache_get(key: str) -> Any | None:
    hit = _cache.get(key)
    if hit:
        stored_at, ttl, value = hit
        if (time.monotonic() - stored_at) < ttl:
            return value
        _cache.pop(key, None)
    return None


def _cache_set(key: str, value: Any, ttl: float) -> None:
    if len(_cache) >= MAX_CACHE_ENTRIES:
        # cheap eviction: drop the oldest entry
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic(), ttl, value)


# --------------------------------------------------------------------------- #
# Error helpers
# --------------------------------------------------------------------------- #
def _err(
    error_type: str,
    message: str,
    *,
    exchange: str | None = None,
    retryable: bool = False,
    supported: list[str] | None = None,
) -> dict:
    payload: dict[str, Any] = {"type": error_type, "message": message, "retryable": retryable}
    if exchange:
        payload["exchange"] = exchange
    if supported is not None:
        payload["supported_exchanges"] = supported
    return {"error": payload}


def _supporting(capability: str) -> list[str]:
    """Which supported exchanges expose a given CCXT capability (no network)."""
    out = []
    for eid in SUPPORTED_EXCHANGES:
        try:
            if getattr(ccxt, eid)().has.get(capability):
                out.append(eid)
        except Exception:  # noqa: BLE001
            pass
    return out


def _make_exchange(eid: str):
    return getattr(ccxt, eid)({"enableRateLimit": True, "timeout": REQUEST_TIMEOUT_MS})


# --------------------------------------------------------------------------- #
# Core executor: validation + capability guard + retries + timeout + cleanup
# --------------------------------------------------------------------------- #
async def _execute(
    exchange_id: str,
    fn: Callable[[Any], Awaitable[dict]],
    *,
    requires: str | None = None,
    cache_key: str | None = None,
    cache_ttl: float = DEFAULT_CACHE_TTL_S,
) -> dict:
    eid = (exchange_id or "").lower().strip()
    if eid not in SUPPORTED_EXCHANGES:
        return _err(
            "invalid_exchange",
            f"Exchange '{exchange_id}' is not supported.",
            supported=SUPPORTED_EXCHANGES,
        )

    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    ex = _make_exchange(eid)
    try:
        if requires and not ex.has.get(requires):
            return _err(
                "not_supported",
                f"'{eid}' does not support '{requires}'.",
                exchange=eid,
                supported=_supporting(requires),
            )

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = await asyncio.wait_for(fn(ex), timeout=OVERALL_TIMEOUT_S)
                # fn may itself return a structured error (e.g. a guard) — don't cache those
                if cache_key and not (isinstance(result, dict) and "error" in result):
                    _cache_set(cache_key, result, cache_ttl)
                return result
            except RETRYABLE_ERRORS as e:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                    log.warning("%s on %s (attempt %d/%d): retrying in %.1fs",
                                type(e).__name__, eid, attempt, MAX_RETRIES, delay)
                    await asyncio.sleep(delay)
                    continue
                return _err("network_error", str(e), exchange=eid, retryable=True)
            except asyncio.TimeoutError:
                return _err("timeout", f"Request to {eid} exceeded {OVERALL_TIMEOUT_S}s.",
                            exchange=eid, retryable=True)
            except ccxt.BadSymbol as e:
                return _err("bad_symbol", str(e), exchange=eid)
            except ccxt.NotSupported as e:
                return _err("not_supported", str(e), exchange=eid, supported=_supporting(requires) if requires else None)
            except ccxt.AuthenticationError as e:
                return _err("auth_error", str(e), exchange=eid)
            except ccxt.ExchangeError as e:
                return _err("exchange_error", str(e), exchange=eid)
            except Exception as e:  # noqa: BLE001 - last resort, never crash the server
                log.exception("Unexpected error on %s", eid)
                return _err("internal_error", str(e), exchange=eid)
        # defensive: unreachable today (loop always returns), but guarantees we
        # never fall through and return None if MAX_RETRIES is ever set to 0.
        return _err("network_error", "Retries exhausted.", exchange=eid, retryable=True)
    finally:
        try:
            await ex.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Discovery tools
# --------------------------------------------------------------------------- #
@mcp.tool
async def list_exchanges() -> dict:
    """List the exchanges supported by this server."""
    return {"exchanges": SUPPORTED_EXCHANGES, "count": len(SUPPORTED_EXCHANGES)}


@mcp.tool
async def get_capabilities() -> dict:
    """Show which data each supported exchange can return (no network call).

    Use this before calling a data tool to pick an exchange that supports it.
    """
    caps = [
        "fetchFundingRate", "fetchFundingRateHistory", "fetchOpenInterest",
        "fetchLiquidations", "fetchTrades", "fetchOHLCV", "fetchOrderBook",
    ]
    matrix: dict[str, dict[str, bool]] = {}
    for eid in SUPPORTED_EXCHANGES:
        has = getattr(ccxt, eid)().has
        matrix[eid] = {c: bool(has.get(c)) for c in caps}
    return {"capabilities": matrix}


@mcp.tool
async def search_symbols(query: str, exchange: str = "bybit", limit: int = 20) -> dict:
    """Find market symbols matching a query (e.g. 'BTC', 'SOL/USDT').

    Useful when you don't know the exact symbol format an exchange expects.

    Args:
        query: Substring to match, case-insensitive.
        exchange: Exchange id (default 'bybit').
        limit: Max symbols to return (default 20, max 100).
    """
    if not query or not query.strip():
        return _err("invalid_input", "query is required, e.g. 'BTC'.")
    n = max(1, min(limit, 100))

    async def fn(ex):
        markets = await ex.load_markets()
        q = query.strip().upper()
        matches = sorted(s for s in markets if q in s.upper())
        return {"exchange": exchange, "query": query, "count": len(matches[:n]),
                "total_matches": len(matches), "symbols": matches[:n]}

    return await _execute(exchange, fn, cache_key=f"mkts:{exchange}:{query}:{n}", cache_ttl=3600)


# --------------------------------------------------------------------------- #
# Price / book / candles
# --------------------------------------------------------------------------- #
@mcp.tool
async def get_price(symbol: str, exchange: str = "bybit") -> dict:
    """Last price and 24h stats for a symbol.

    Args:
        symbol: 'BTC/USDT' (spot) or 'BTC/USDT:USDT' (perp).
        exchange: Exchange id (default 'bybit').
    """
    async def fn(ex):
        t = await ex.fetch_ticker(symbol)
        return {
            "exchange": exchange, "symbol": symbol,
            "last": t.get("last"), "bid": t.get("bid"), "ask": t.get("ask"),
            "high_24h": t.get("high"), "low_24h": t.get("low"),
            "change_pct_24h": t.get("percentage"),
            "base_volume_24h": t.get("baseVolume"), "timestamp": t.get("datetime"),
        }

    return await _execute(exchange, fn, cache_key=f"price:{exchange}:{symbol}", cache_ttl=3)


@mcp.tool
async def get_order_book(symbol: str, exchange: str = "bybit", depth: int = 5) -> dict:
    """Top-of-book bids and asks.

    Args:
        symbol: e.g. 'BTC/USDT:USDT'.
        exchange: Exchange id (default 'bybit').
        depth: Levels per side (default 5, max 50).
    """
    d = max(1, min(depth, 50))

    async def fn(ex):
        ob = await ex.fetch_order_book(symbol, limit=d)
        return {"exchange": exchange, "symbol": symbol,
                "bids": ob.get("bids", [])[:d], "asks": ob.get("asks", [])[:d],
                "timestamp": ob.get("datetime")}

    return await _execute(exchange, fn, requires="fetchOrderBook",
                          cache_key=f"ob:{exchange}:{symbol}:{d}", cache_ttl=2)


@mcp.tool
async def get_ohlcv(symbol: str, exchange: str = "bybit", timeframe: str = "1h", limit: int = 24) -> dict:
    """Recent OHLCV candles.

    Args:
        symbol: e.g. 'BTC/USDT:USDT'.
        exchange: Exchange id (default 'bybit').
        timeframe: '1m','5m','15m','1h','4h','1d', etc.
        limit: Number of candles (default 24, max 200).
    """
    n = max(1, min(limit, 200))

    async def fn(ex):
        rows = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=n)
        candles = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                    "close": r[4], "volume": r[5]} for r in rows]
        return {"exchange": exchange, "symbol": symbol, "timeframe": timeframe,
                "count": len(candles), "candles": candles}

    return await _execute(exchange, fn, requires="fetchOHLCV",
                          cache_key=f"ohlcv:{exchange}:{symbol}:{timeframe}:{n}", cache_ttl=10)


@mcp.tool
async def get_recent_trades(symbol: str, exchange: str = "bybit", limit: int = 20) -> dict:
    """Most recent public trades for a symbol.

    Args:
        symbol: e.g. 'BTC/USDT:USDT'.
        exchange: Exchange id (default 'bybit').
        limit: Number of trades (default 20, max 100).
    """
    n = max(1, min(limit, 100))

    async def fn(ex):
        rows = await ex.fetch_trades(symbol, limit=n)
        trimmed = rows[-n:]
        return {"exchange": exchange, "symbol": symbol, "count": len(trimmed),
                "trades": [{"ts": t.get("timestamp"), "side": t.get("side"),
                            "price": t.get("price"), "amount": t.get("amount")} for t in trimmed]}

    return await _execute(exchange, fn, requires="fetchTrades",
                          cache_key=f"trades:{exchange}:{symbol}:{n}", cache_ttl=2)


# --------------------------------------------------------------------------- #
# Derivatives: funding, open interest, long/short, liquidations
# --------------------------------------------------------------------------- #
@mcp.tool
async def get_funding_rate(symbol: str, exchange: str = "bybit") -> dict:
    """Current perpetual funding rate for a symbol.

    Args:
        symbol: Perp symbol, e.g. 'BTC/USDT:USDT'.
        exchange: Exchange id (default 'bybit').
    """
    async def fn(ex):
        fr = await ex.fetch_funding_rate(symbol)
        return {"exchange": exchange, "symbol": symbol,
                "funding_rate": fr.get("fundingRate"),
                "funding_timestamp": fr.get("fundingDatetime"),
                "next_funding_rate": fr.get("nextFundingRate"),
                "mark_price": fr.get("markPrice"), "index_price": fr.get("indexPrice")}

    return await _execute(exchange, fn, requires="fetchFundingRate",
                          cache_key=f"fr:{exchange}:{symbol}", cache_ttl=10)


@mcp.tool
async def get_funding_rate_history(symbol: str, exchange: str = "bybit", limit: int = 10) -> dict:
    """Historical funding rates for a perp symbol (newest last).

    Args:
        symbol: Perp symbol, e.g. 'BTC/USDT:USDT'.
        exchange: Exchange id (default 'bybit').
        limit: Number of records (default 10, max 100).
    """
    n = max(1, min(limit, 100))

    async def fn(ex):
        rows = await ex.fetch_funding_rate_history(symbol, limit=n)
        trimmed = rows[-n:]
        return {"exchange": exchange, "symbol": symbol, "count": len(trimmed),
                "history": [{"ts": r.get("timestamp"), "datetime": r.get("datetime"),
                             "funding_rate": r.get("fundingRate")} for r in trimmed]}

    return await _execute(exchange, fn, requires="fetchFundingRateHistory",
                          cache_key=f"frh:{exchange}:{symbol}:{n}", cache_ttl=30)


@mcp.tool
async def compare_funding(symbol: str = "BTC/USDT:USDT", exchanges: list[str] | None = None) -> dict:
    """Compare funding rates for one perp symbol across exchanges (concurrent).

    Surfaces funding-rate arbitrage: the venue paying the most vs the least,
    and the spread between them.

    Args:
        symbol: Perp symbol, e.g. 'BTC/USDT:USDT'.
        exchanges: List of exchange ids. Defaults to all supported.
    """
    targets = exchanges or SUPPORTED_EXCHANGES

    async def fetch_one(eid: str) -> dict:
        async def fn(ex):
            fr = await ex.fetch_funding_rate(symbol)
            return {"exchange": eid, "funding_rate": fr.get("fundingRate")}

        res = await _execute(eid, fn, requires="fetchFundingRate",
                             cache_key=f"fr:{eid}:{symbol}", cache_ttl=10)
        if "error" in res:
            return {"exchange": eid, "error": res["error"]["message"]}
        return res

    results = await asyncio.gather(*[fetch_one(e) for e in targets])

    valid = [r for r in results if r.get("funding_rate") is not None]
    arb_spread = None
    if len(valid) >= 2:
        hi = max(valid, key=lambda r: r["funding_rate"])
        lo = min(valid, key=lambda r: r["funding_rate"])
        arb_spread = {
            "max": {"exchange": hi["exchange"], "rate": hi["funding_rate"]},
            "min": {"exchange": lo["exchange"], "rate": lo["funding_rate"]},
            "spread": hi["funding_rate"] - lo["funding_rate"],
        }

    return {"symbol": symbol, "rates": results, "arb_spread": arb_spread}


@mcp.tool
async def get_open_interest(symbol: str, exchange: str = "bybit") -> dict:
    """Current open interest for a perp symbol.

    Args:
        symbol: Perp symbol, e.g. 'BTC/USDT:USDT'.
        exchange: Exchange id (default 'bybit'). Note: 'gate' does not support this.
    """
    async def fn(ex):
        oi = await ex.fetch_open_interest(symbol)
        return {"exchange": exchange, "symbol": symbol,
                "open_interest_amount": oi.get("openInterestAmount"),
                "open_interest_value": oi.get("openInterestValue"),
                "timestamp": oi.get("datetime")}

    return await _execute(exchange, fn, requires="fetchOpenInterest",
                          cache_key=f"oi:{exchange}:{symbol}", cache_ttl=10)


@mcp.tool
async def get_long_short_ratio(
    symbol: str = "BTCUSDT", period: str = "5m", limit: int = 10, exchange: str = "binance"
) -> dict:
    """Global long/short account ratio (Binance USDⓈ-M futures only).

    Args:
        symbol: Binance raw symbol, e.g. 'BTCUSDT' (no slash).
        period: '5m','15m','30m','1h','2h','4h','6h','12h','1d'.
        limit: Number of data points (default 10, max 500).
        exchange: Must be 'binance' (only supported venue for this metric here).
    """
    if exchange.lower().strip() != "binance":
        return _err("not_supported",
                    "long/short ratio is available on 'binance' only in this server.",
                    supported=["binance"])
    n = max(1, min(limit, 500))

    async def fn(ex):
        if not hasattr(ex, "fapiDataGetGlobalLongShortAccountRatio"):
            return _err("not_supported", "This ccxt build lacks the Binance long/short endpoint.",
                        exchange="binance")
        data = await ex.fapiDataGetGlobalLongShortAccountRatio(
            {"symbol": symbol, "period": period, "limit": n}
        )
        return {"exchange": "binance", "symbol": symbol, "period": period,
                "count": len(data),
                "data": [{"ts": int(d["timestamp"]),
                          "long_short_ratio": float(d["longShortRatio"]),
                          "long_pct": float(d["longAccount"]),
                          "short_pct": float(d["shortAccount"])} for d in data]}

    return await _execute("binance", fn, cache_key=f"lsr:{symbol}:{period}:{n}", cache_ttl=30)


@mcp.tool
async def get_liquidations(symbol: str, exchange: str = "gate", limit: int = 20) -> dict:
    """Recent public liquidations for a perp symbol.

    Args:
        symbol: Perp symbol, e.g. 'BTC/USDT:USDT'.
        exchange: Exchange id (default 'gate'). Support is limited — see get_capabilities.
        limit: Number of records (default 20, max 100).
    """
    n = max(1, min(limit, 100))

    async def fn(ex):
        rows = await ex.fetch_liquidations(symbol, limit=n)
        trimmed = rows[-n:]
        return {"exchange": exchange, "symbol": symbol, "count": len(trimmed),
                "liquidations": [{"ts": r.get("timestamp"), "side": r.get("side"),
                                  "price": r.get("price"), "amount": r.get("amount")} for r in trimmed]}

    return await _execute(exchange, fn, requires="fetchLiquidations",
                          cache_key=f"liq:{exchange}:{symbol}:{n}", cache_ttl=10)


def main() -> None:
    """Entry point. HTTP mode enables auth + rate limiting; stdio stays open."""
    if HTTP_MODE:
        import os

        from ratelimit import RateLimitMiddleware

        mcp.add_middleware(RateLimitMiddleware())
        mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    else:
        mcp.run()


if __name__ == "__main__":
    main()

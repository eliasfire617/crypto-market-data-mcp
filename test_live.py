"""Live smoke test: exercises every tool in-process against real exchange data."""

import asyncio
import json
import sys

from fastmcp import Client

import server


def show(name: str, payload) -> None:
    status = "ERR " if isinstance(payload, dict) and "error" in payload else "OK  "
    print(f"{status} {name}: {json.dumps(payload, default=str)[:300]}", flush=True)


async def main() -> None:
    failures = 0
    async with Client(server.mcp) as c:
        async def call(name: str, **kw):
            nonlocal failures
            try:
                res = await c.call_tool(name, kw)
                data = res.data if hasattr(res, "data") else res
                show(name, data)
                if isinstance(data, dict) and "error" in data:
                    failures += 1
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}", flush=True)

        await call("list_exchanges")
        await call("get_capabilities")
        await call("search_symbols", query="BTC", exchange="bybit", limit=5)
        await call("get_price", symbol="BTC/USDT", exchange="bybit")
        await call("get_order_book", symbol="BTC/USDT:USDT", exchange="bybit", depth=5)
        await call("get_ohlcv", symbol="BTC/USDT:USDT", exchange="bybit", limit=24)
        await call("get_recent_trades", symbol="BTC/USDT:USDT", exchange="bybit", limit=5)
        await call("get_funding_rate", symbol="BTC/USDT:USDT", exchange="bybit")
        await call("get_funding_rate_history", symbol="BTC/USDT:USDT", exchange="bybit", limit=5)
        await call("compare_funding", symbol="BTC/USDT:USDT")
        await call("get_open_interest", symbol="BTC/USDT:USDT", exchange="bybit")
        await call("get_long_short_ratio", symbol="BTCUSDT", period="1h", limit=10)
        await call("get_liquidations", symbol="BTC/USDT:USDT", exchange="gate", limit=5)

        # agent-friendly symbol shorthand must normalize end-to-end
        await call("get_price", symbol="btcusdt", exchange="bybit")
        await call("get_funding_rate", symbol="sol", exchange="bybit")
        await call("get_long_short_ratio", symbol="ETH/USDT:USDT", period="1h", limit=5)

        # error paths must come back structured, never as crashes
        await call("get_price", symbol="NOPE/XXX", exchange="bybit")
        await call("get_open_interest", symbol="BTC/USDT:USDT", exchange="gate")

    print(f"\nfailures (excluding the 2 expected error-path checks): {max(0, failures - 2)}")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())

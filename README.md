# Crypto Market Data MCP Server
[![MCPize](https://mcpize.com/badge/@eliasfire617/crypto-market-data)](https://mcpize.com/mcp/crypto-market-data)

A **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server that
gives any AI agent (Claude, Cursor, Cline, Windsurf…) live cryptocurrency market data
across 100+ exchanges via [CCXT](https://github.com/ccxt/ccxt). No API keys required —
everything uses public endpoints.

Supported exchanges: **bybit, binance, okx, hyperliquid, gate, kucoin** (add more
CCXT ids in `SUPPORTED_EXCHANGES`).

## Connect via MCPize

Use this MCP server instantly with no local installation:

```bash
npx -y mcpize connect @eliasfire617/crypto-market-data --client claude
```

Or connect at: **https://mcpize.com/mcp/crypto-market-data**

## Tools (13)

**Discovery**
| Tool | What it does |
|------|--------------|
| `list_exchanges` | Which exchanges are supported |
| `get_capabilities` | Matrix of which exchange supports which data (no network) |
| `search_symbols` | Find the exact symbol format for a coin |

**Market data**
| Tool | What it does |
|------|--------------|
| `get_price` | Last price + 24h stats |
| `get_order_book` | Top-of-book bids/asks |
| `get_ohlcv` | Recent OHLCV candles |
| `get_recent_trades` | Latest public trades |

**Derivatives**
| Tool | What it does |
|------|--------------|
| `get_funding_rate` | Current perpetual funding rate |
| `get_funding_rate_history` | Historical funding rates |
| `compare_funding` | Funding rate across many exchanges + **arb spread** |
| `get_open_interest` | Current open interest |
| `get_long_short_ratio` | Global long/short account ratio (Binance) |
| `get_liquidations` | Recent public liquidations (limited support) |

`compare_funding` is the differentiator: it surfaces funding-rate arbitrage
opportunities (which venue pays the most vs least) in one concurrent call.

## Robustness

Built for production, not a demo:

- **Retries with exponential backoff** on transient network errors (3 attempts).
- **Per-request + overall timeouts** (10s / 25s) so a hung exchange can't stall a call.
- **Capability guards**: never calls a method an exchange lacks — returns a clean
  error listing which exchanges *do* support it (see `get_capabilities`).
- **Structured errors** (`{"error": {type, message, retryable, supported_exchanges}}`)
  instead of crashes — agents always get a parseable response.
- **TTL response cache** (~660x faster on repeat calls; eases rate limits).
- **Input validation** and clamped limits on every tool.
- **Guaranteed exchange cleanup**; logs to **stderr only** (safe for stdio transport).

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# stdio transport (Claude Desktop, Cursor, Cline) — local, no auth:
python server.py

# OR streamable HTTP (hosted / remote) — API-key auth + rate limiting ON:
python server.py --http                 # binds :8000 (override with PORT=8799)
```

## Authentication & billing (HTTP mode)

The hosted HTTP transport is **gated** — this is what makes it billable. Local
stdio stays open for single-user dev.

- Clients send `Authorization: Bearer <api-key>`.
- Each key maps to a `client_id` and a **tier**; unknown/missing keys get `401`.
- Tiers (`auth.py` → `TIERS`) set per-minute rate + monthly quota:

  | Tier | req/min | monthly quota |
  |------|--------:|--------------:|
  | free | 10 | 1,000 |
  | pro | 120 | 100,000 |
  | ultra | 600 | 2,000,000 |

- Over the limit → a clean `ToolError` (`Rate limit exceeded…` / `Monthly quota
  exceeded…`), never a crash.

**Configure keys** (either source; file wins):

```bash
# inline
export CRYPTO_MCP_API_KEYS="sk_live_abc:acme:pro,sk_live_xyz:jane:free"
# or a file (see api_keys.example.json)
export CRYPTO_MCP_API_KEYS_FILE=./api_keys.json
```

If no keys are set in HTTP mode, a throwaway **demo key (tier=pro)** is generated
and printed to stderr so you can test immediately.

### Use with Claude Desktop

Copy `claude_desktop_config.example.json` into your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows), adjusting paths.

### Symbols

- Spot: `BTC/USDT`
- Perpetual: `BTC/USDT:USDT`

---

## Roadmap

- Add more exchanges to `SUPPORTED_EXCHANGES`.
- More aggressive caching of hot symbols to cut latency.
- Optional websocket streaming for live order book / trades.

---

*Maintainers: hosting, deployment and registry-listing notes live in [PUBLISHING.md](PUBLISHING.md).*
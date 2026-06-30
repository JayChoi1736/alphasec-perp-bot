# alphasec-perp-bot

Minimal Python bots that drive a perp DEX core node directly over JSON-RPC —
for market-making, taker flow, orderbook watching, and load testing. Not a
trading strategy; it exercises the core matching / funding / settlement paths.

A DEX tx is `0x<cmd byte><utf8 JSON>` sent to the DEX precompile (`0x…cc`),
ported from `nitro-testnode/tests/helpers.js`.

## Files
| file | what it does |
|------|--------------|
| `dex.py` | `PerpDexClient` — encode/sign/send DEX commands, read RPC (account, lvl2, oracle, market info) |
| `maker.py` | multi-level ladder market-maker (mark-band clamped, drift-based requote) |
| `taker.py` | alternating IOC crosser |
| `watch.py` | live orderbook depth/trade stream over WebSocket (ms timestamps) |
| `load.py` | **throughput** load — N accounts spam IOC orders to hit a target tx/s |
| `match.py` | **matched** load — maker/taker account pairs producing real fills, measures trade/s |
| `accounts.py` | persistent sub-account keystore + pre-flight gas/deposit top-up |
| `setup_dev.py` | local-dev helper: oracle price + fund the taker account |
| `test_encode.py` | wire-format byte check (no deps) |

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json          # QA template
# cp config.dev.example.json config.dev.json # local dev template
```
Real configs (`config.json`, `config.dev.json`) hold private keys and are
**git-ignored**. Only the `*.example.json` templates (placeholder keys) are committed.

## Config
One file, common keys at top level + `maker`/`taker` role blocks (separate
accounts so they match each other):
```jsonc
{
  "rpc_url": "http://localhost:8547", "dex_address": "0x…cc",
  "market_id": 1, "leverage": 10, "margin_type": 0, "interval": 2, "ref_price": 40000,
  "maker": { "private_key": "0x…", "deposit": 0, "spread": 0.001,
             "order_size": 0.01, "levels": 5, "level_step": 0.001 },
  "taker": { "private_key": "0x…", "deposit": 0, "order_size": 0.01, "taker_slippage": 0.01 }
}
```
| key | meaning |
|-----|---------|
| `deposit` | 0 = skip (already funded); >0 = auto-deposit token 2 from spot |
| `interval` | seconds between maker/taker ticks |
| `ref_price` | fallback mid when the book + oracle are empty |
| maker `spread` / `levels` / `level_step` | inner offset, ladder depth per side, gap between levels |
| `taker_slippage` | how far past the touch the taker crosses |
| `tps`, `load_tps`, `per_account_tps`, `match_tps`, `match_deposit`, `inventory_cap` | load-test knobs (below) |

`tick_size`/`lot_size`/price-band are read live from the registry (`getMarket`,
with an ABI fallback); config values are only used if that call fails.

## Run the bots
```bash
.venv/bin/python maker.py config.json    # ladder quotes, re-quotes on drift
.venv/bin/python taker.py config.json    # IOC crosses, alternating side
```
Both, at startup: optional deposit → cancel stale orders → set leverage/margin
→ **preflight margin check** → loop. Every tick they read the **mark-price band**
and keep quotes strictly inside it.

## Watch the orderbook (WebSocket)
```bash
.venv/bin/python watch.py config.json
```
Streams depth diffs + fills with millisecond timestamps (no polling):
```
17:09:42.658 [depth m1] bids=[['39960','0.01']] asks=[]   # level added
17:09:42.911 [depth m1] bids=[] asks=[['40040','0']]       # level removed (cancel or fill)
```
Needs the node started with WebSocket enabled (see dev note). Set `market_id` to 0 to watch all markets.

## Local dev node
The node must serve HTTP + WS:
```bash
nitro --dev --init.dev-init-address=0x9e0C…9315 \
  --http.addr=0.0.0.0 --http.api=eth,net,web3,arb,arbdebug,debug,admin,txpool \
  --ws.addr=0.0.0.0 --ws.port=8548 --ws.api=eth,net,web3,arb,debug
```
(nitro has no bare `--ws`; `--ws.addr` enables it.) Then:
```bash
.venv/bin/python setup_dev.py config.dev.json   # grant OracleSubmitter, set oracle, fund taker
.venv/bin/python maker.py config.dev.json &
.venv/bin/python taker.py config.dev.json
```
A fresh `--dev` node has no markets — register one first (e.g. via
`nitro-testnode/tests` helpers `addMarket`); the first market is id 1.

## Load testing

### Submission (async send)
Fire-and-forget orders use **`eth_sendRawTransactionAsync`**, which enqueues a
tx and returns immediately instead of blocking until it is sequenced. That lifts
a single account from ~10 tx/s (sync `eth_sendRawTransaction`, which waits ~per
block) to ~70 tx/s, so a single process easily sustains 100–150+ tx/s. Both load
tools still fan out across N accounts (`ceil(target / per_account_tps)`); with
async send `per_account_tps` can be raised well above the old ~4 to use fewer
accounts for the same target.

### Sub-accounts (`accounts.py`)
Sub-account keys are persisted in `accounts.json` (git-ignored — holds private
keys) and reused across runs. Each launch the driver ensures the keystore has
enough accounts (generating + saving any missing) and, before starting, checks
each account's balance and **tops up gas + perp deposit to target** (the
"rebalance first" step). Set `keystore` in config to use a different file.

### A) Throughput — `load.py`
N accounts fire-and-forget IOC orders (cross the band, expire with no fill =
pure submission load, no margin/state). `N = ceil(tps / per_account_tps)`.
```bash
.venv/bin/python load.py config.dev.json <target_tps> [duration_s]
```
Measured (dev node, verified by counting on-chain DEX txs = submissions):
| target | accounts | achieved | on-chain match |
|--------|----------|----------|----------------|
| 20  | 5  | 19.8 tx/s | ✓ |
| 50  | 13 | 50.0 tx/s (5 min) | ✓ 15028≈15041 |
| 100 | 25 | 98.7 tx/s (5 min) | ✓ 29635=29635 |

### B) Matched — `match.py`
Splits accounts into maker/taker pairs producing **real fills**, with inventory
rebalancing so it runs indefinitely:
- makers quote both sides, skipping the side that would push them past `inventory_cap`;
- takers hold a direction until they hit `±inventory_cap`, then reverse — so each
  position sweeps a triangle wave inside the band and **margin never grows**.
```bash
.venv/bin/python match.py config.dev.json <target_tx_s> [duration_s]   # duration 0 = until Ctrl-C
```
The target is **total tx/s** (submission rate, not fills). It fans out to
`ceil(target / per_account_tps / 2)` maker + taker accounts, each paced to emit
`per_account_tps` tx/s; the progress line reports both measured `tx_rate` and the
resulting `trade_rate` (fills). Accuracy depends on `per_account_tps` matching the
env's real per-account send ceiling (≈4 on a local dev node, ≈2.7 on a remote
RPC) — set it lower to add accounts and hit a higher target.

A background poller snapshots positions each second so the order loops never
block on RPC; fills are counted as cumulative |position change| on the taker side.
Relevant config: `match_deposit`, `inventory_cap`, `per_account_tps`, `spread`
(maker offset, must be < `taker_slippage`), and `match_levels` / `level_step` —
makers post a ladder of `match_levels` price points per side (default 1) for a
richer, deeper-looking book instead of a single bid/ask price.

Activity-mix knobs (tx/s stays at the limiter target regardless):
- `taker_cross_every` (default 1) — takers cross (fill) only every Nth order; the
  rest are non-crossing IOCs (pure tx load). Trade rate ≈ (target/2) / N, so e.g.
  N=50 at 200 tx/s gives ~1–2 trades/s.
- `maker_cancel_every` (default 6) — makers issue an individual `PERP_CANCEL`
  (by order tx-hash) every Nth op for realistic place/cancel churn; a cancel_all
  safety net still runs every 60 ops to bound resting-order margin.

## Wire encoding (per command, verified against a live node)
| field | encoding |
|-------|----------|
| order `price` / `quantity` | **human decimal string** (`"39960"`, `"0.01"`) — engine scales ×1e18 |
| deposit `amount` | **wei integer string** (uint256) |
| transfer `value` | human decimal string |
| `marketId` / `side` / `leverage` / `timeInForce` | bare ints |

side: 0=buy 1=sell · timeInForce: 0=GTC 1=IOC 2=POST 3=MARKET · marginType: 0=cross 1=isolated

## Test
```bash
.venv/bin/python test_encode.py   # wire-format byte check (no deps)
```

## Skipped (add when needed)
Inventory/risk logic, PnL tracking, reconnect/recovery, TP/SL, modify,
multi-market, reduce-only unwinding in `match.py`. Each is a method or a few
lines on `PerpDexClient`.

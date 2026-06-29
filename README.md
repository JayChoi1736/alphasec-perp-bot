# alphasec-perp-bot

Minimal perp maker/taker bots that talk straight to a core node over JSON-RPC.
Primary use: generate maker/taker flow to exercise the core matching/funding/
settlement paths (local dev node or QA). Not a trading strategy.

A DEX tx is `0x<cmd byte><utf8 JSON>` sent to the DEX precompile — ported from
`nitro-testnode/tests/helpers.js`. Per-command field encoding (verified against a
live node, see `dex.py` header):

| field | encoding |
|-------|----------|
| order `price`/`quantity` | **human decimal string** (`"39960"`, `"0.01"`) — engine scales ×1e18 |
| deposit `amount` | **wei integer string** (uint256) |
| transfer `value` | human decimal string |
| `marketId`/`side`/`leverage`/`timeInForce` | bare ints |

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json          # QA template; fill in private keys
# cp config.dev.example.json config.dev.json # local dev template
```
Real configs (`config.json`, `config.dev.json`, …) hold private keys and are
**git-ignored**. Only the `*.example.json` templates (placeholder keys) are committed.

## Config (one file, two roles)
Common keys at top level; `maker` / `taker` blocks override per role (each is a
separate account so they actually match each other):
```jsonc
{
  "rpc_url": "...", "dex_address": "0x..cc", "market_id": 24,
  "leverage": 10, "margin_type": 0, "interval": 3, "ref_price": 84,
  "maker": { "private_key": "0x..", "deposit": 0, "spread": 0.001,
             "order_size": 0.5, "levels": 5, "level_step": 0.001 },
  "taker": { "private_key": "0x..", "deposit": 0, "order_size": 0.5, "taker_slippage": 0.01 }
}
```
- `deposit`: 0 = skip (account already funded), >0 = auto-deposit token 2 from spot.
- `tick_size`/`lot_size`/band are read live from the registry (`getMarket`, with an
  ABI fallback) — config values are only used if that call fails.
- maker `levels`/`level_step`: post a ladder of N price levels per side, stepped by
  `level_step`; the maker only re-quotes when mid drifts, so a quiet book stays full.
- `tps` (optional, either role): switch to **load mode** — fire-and-forget orders
  paced to that submit rate (no per-order receipt wait). See below.

Each bot, at startup: deposit (optional) → cancel stale orders → set leverage/margin
→ **preflight margin check** (exits if free margin < what its quotes need).
Each tick it re-reads the **mark price band** and keeps quotes strictly inside it.

## Load mode (`tps`)
Set `tps` on a role to drive a target submit rate. The node caps a *single* account
at ~1 tx/block (≈4 tx/s on a 250 ms dev node) — submission scales linearly with
sender accounts, so for higher aggregate TPS run the bot from several funded keys
in parallel (~4 × N tx/s).

## Run
```bash
.venv/bin/python maker.py config.json   # quotes both sides, refreshes each tick
.venv/bin/python taker.py config.json   # crosses IOC, alternating side
```

## Local dev node
```bash
.venv/bin/python setup_dev.py config.dev.json   # oracle price + fund taker (gas + token 2)
.venv/bin/python maker.py config.dev.json &
.venv/bin/python taker.py config.dev.json
```
`config.dev.json` (from `config.dev.example.json`) targets `http://localhost:8547`,
market 22; maker uses the nitro `--dev` account, taker a second funded key.
Verified end-to-end: maker quotes rest, taker crosses, fills open real positions
and move margin/PnL.

## Test
```bash
.venv/bin/python test_encode.py   # wire-format byte check (no deps)
```

## Skipped (add when needed)
Inventory/risk logic, PnL tracking, reconnect/recovery, TP/SL, modify,
multi-market. Each is a method or a few lines on `PerpDexClient`.

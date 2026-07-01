#!/usr/bin/env python3
"""Binance-backed oracle feeder — real prices for every registered market.

Signs with the OracleSubmitter admin key. Two cadences:
  - every `market_refresh` s (default 300): read the perp market count from the
    registry and map each marketId -> a Binance symbol (from its on-chain symbol,
    or by popularity rank as a fallback).
  - every `push_interval` s (default 3): pull those prices from Binance in one
    request and submit a single oracle BATCH tx (cmd 0x4D) for all markets.

  python -m bots.binance_oracle_feeder [configs/config.json]   ('binance_feeder' role)

interval must exceed block time; mark updates land in block N+1 (deferred scan).
"""
import json
import sys
import time
import urllib.parse
import urllib.request

from lib.dex import PerpDexClient, load_role_config, MAX_ORACLE_BATCH

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
# popularity-ranked fallback when a market's on-chain symbol can't be mapped
DEFAULT_POPULARITY = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX",
                      "LINK", "TRX", "DOT", "MATIC", "LTC", "SHIB", "BCH", "UNI"]


def base_asset(symbol):
    """On-chain market symbol -> base asset. 'BTC-USD'->'BTC', 'ETHUSDT'->'ETH'."""
    if not symbol:
        return None
    s = symbol.upper().split("-")[0].split("_")[0].split("/")[0]
    for quote in ("USDT", "USDC", "USD", "PERP"):
        if s.endswith(quote) and len(s) > len(quote):
            s = s[: -len(quote)]
    return s or None


def build_market_map(client, popularity, quote):
    """{marketId: binance_symbol} for markets 1..count, on-chain symbol first."""
    n = client.market_count()
    out = {}
    for mid in range(1, n + 1):
        info = client.market_info(mid)
        base = base_asset(info.get("symbol")) if info else None
        if not base:  # fallback: popularity rank
            base = popularity[mid - 1] if mid - 1 < len(popularity) else None
        if base:
            out[mid] = base + quote
    return out, n


def fetch_prices(symbols):
    """One Binance request -> {binance_symbol: float price} for the given symbols."""
    if not symbols:
        return {}
    # Binance rejects whitespace inside the symbols array -> compact separators
    q = urllib.parse.urlencode({"symbols": json.dumps(sorted(set(symbols)), separators=(",", ":"))})
    with urllib.request.urlopen(f"{BINANCE_TICKER}?{q}", timeout=5) as r:
        rows = json.loads(r.read().decode())
    return {row["symbol"]: float(row["price"]) for row in rows}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.json"
    cfg = load_role_config(path, "binance_feeder")
    c = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    push_interval = float(cfg.get("push_interval", 3))
    market_refresh = float(cfg.get("market_refresh", 300))
    popularity = cfg.get("symbols", DEFAULT_POPULARITY)
    quote = cfg.get("quote", "USDT")

    print(f"binance feeder {c.address} push={push_interval}s refresh={market_refresh}s quote={quote}")
    if not c.is_oracle_submitter():
        if cfg.get("auto_register"):  # dev: the key is ChainOwner, so it can self-grant
            try:
                c.add_oracle_submitter(c.address)
                print(f"registered {c.address} as OracleSubmitter (auto_register)")
            except Exception as e:
                print(f"WARNING: auto_register failed ({e}) — key is not ChainOwner? txs will revert.")
        else:
            print(f"WARNING: {c.address} is NOT a registered OracleSubmitter — txs will revert.")

    market_map, last_refresh = {}, None
    while True:
        try:
            if last_refresh is None or time.time() - last_refresh >= market_refresh:
                market_map, n = build_market_map(c, popularity, quote)
                last_refresh = time.time()
                print(f"markets refreshed: count={n} feeding={market_map}")

            prices = fetch_prices(market_map.values())
            entries = [(mid, prices[sym]) for mid, sym in market_map.items() if sym in prices]
            missing = [sym for sym in market_map.values() if sym not in prices]
            if missing:
                print("no Binance price for:", sorted(set(missing)))
            # chunk to the batch cap; each chunk is one 0x4D tx
            for i in range(0, len(entries), MAX_ORACLE_BATCH):
                chunk = entries[i:i + MAX_ORACLE_BATCH]
                r = c.oracle_price_batch(chunk)
                print(f"batch n={len(chunk)} block={r.blockNumber} status={r.status} "
                      f"prices={{{', '.join(f'{m}:{p}' for m, p in chunk)}}}")
        except Exception as e:  # ponytail: log-and-continue; a bad tick shouldn't kill the feed
            print("tick error:", e)
        time.sleep(push_interval)


if __name__ == "__main__":
    main()

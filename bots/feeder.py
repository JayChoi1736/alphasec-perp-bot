#!/usr/bin/env python3
"""Oracle feeder bot — pushes perp mark prices on a fixed interval.

A "tick" is one loop pass: pick a price per market -> submit oracle tx -> sleep
`interval`. Mark price updates land in block N+1 (deferred scan), not the oracle
tx's block. See confluence "Oracle Feeder Process".

Price source (config "source"):
  - "constant"   : ref_price, unchanged (default)
  - "randomwalk" : ref_price * (1 ± vol) drift each tick — moves the mark so
                   funding/liquidation paths get exercised on a dev node
  - "book"       : best-bid/ask mid from the live book (falls back to ref_price)

  python -m bots.feeder [configs/config.json]    (reads the 'feeder' role block)

interval MUST exceed block time, else same-block submissions collapse
(last-writer-wins) and intermediate audit logs are lost.
"""
import random
import sys
import time

from lib.dex import PerpDexClient, load_role_config


def next_price(source, prev, ref, vol, client, market_id):
    if source == "randomwalk":
        return round(prev * (1 + random.uniform(-vol, vol)), 8)
    if source == "book":
        bb, ba = client.best_bid_ask(market_id)
        return (bb + ba) / 2 if bb and ba else (bb or ba or ref)
    return ref  # constant


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.json"
    cfg = load_role_config(path, "feeder")
    c = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])

    # markets: explicit list, or fall back to the top-level single market_id
    markets = cfg.get("markets") or [int(cfg["market_id"])]
    ref = float(cfg["ref_price"])
    source = cfg.get("source", "constant")
    vol = float(cfg.get("vol", 0.002))          # per-tick drift for randomwalk
    interval = float(cfg.get("interval", 3))
    use_batch = cfg.get("batch", len(markets) > 1)

    print(f"feeder {c.address} markets={markets} source={source} ref={ref} "
          f"interval={interval}s batch={use_batch}")
    if not c.is_oracle_submitter():
        print(f"WARNING: {c.address} is NOT a registered OracleSubmitter — txs will "
              f"revert. Have ChainOwner call add_oracle_submitter (ArbOwner, V3+).")

    px = {m: ref for m in markets}  # per-market last price (randomwalk state)
    while True:
        try:
            px = {m: next_price(source, px[m], ref, vol, c, m) for m in markets}
            if use_batch:
                r = c.oracle_price_batch([(m, px[m]) for m in markets])
                print(f"batch n={len(markets)} prices={px} block={r.blockNumber} status={r.status}")
            else:
                for m in markets:
                    r = c.oracle_price(m, px[m])
                    print(f"market={m} price={px[m]} block={r.blockNumber} status={r.status}")
        except Exception as e:  # ponytail: log-and-continue; a bad tick shouldn't kill the feed
            print("tick error:", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()

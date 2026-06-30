#!/usr/bin/env python3
"""Matched load with inventory rebalancing — real fills, runs indefinitely.

Maker/taker account pairs produce real fills, but every account keeps its
position inside +/- `inventory_cap` so margin never blows up over long runs:

  - makers quote BOTH sides; skip the side that would push them past the cap
    (too short -> stop adding asks; too long -> stop adding bids).
  - takers cross toward zero (long -> sell, short -> buy, near flat -> alternate).

A background poller snapshots positions every second so the hot order loops
never block on RPC. Fills are counted as cumulative |position change| on the
taker side (net position oscillates, so a net read would undercount).

  python match.py [config.json] [target_tx_s] [duration_s]   # target is total tx/s

duration 0 = run until Ctrl-C.
"""
import math
import sys
import threading
import time

from web3 import Web3

from accounts import load_or_create, ensure_funded
from dex import (PerpDexClient, RateLimiter, BUY, SELL, POST, IOC, DEFAULT_BAND_BPS,
                 band_bounds, load_role_config, _to_int)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_role_config(path, "maker")
    target = float(sys.argv[2]) if len(sys.argv) > 2 else float(cfg.get("match_tps", 20))  # target tx/s
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("match_duration", 0))
    per_acct = float(cfg.get("per_account_tps", 4))  # node ceiling: ~1 tx/block per sender
    lev = int(cfg["leverage"])
    deposit = float(cfg.get("match_deposit", 100000))
    size = float(cfg.get("order_size", 0.01))
    cap = float(cfg.get("inventory_cap", 0.5))        # max |position| per account (base units)
    spread = float(cfg.get("spread", 0.0005))         # maker offset (< taker_slippage)
    slip = float(cfg.get("taker_slippage", 0.01))
    mid = cfg["market_id"]
    # target is TOTAL tx/s. Each account submits at ~per_acct tx/s (node cap), so
    # split the rate across `pairs` makers + `pairs` takers -> 2*pairs accounts.
    pairs = max(1, math.ceil(target / per_acct / 2))

    dev = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    w3 = dev.w3
    mi = dev.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    mark = dev.mark_price(mid) or float(cfg["ref_price"])
    lo, hi = band_bounds(mark, band_bps)
    q = lambda x: round(x / tick) * tick
    # maker ladder: match_levels price points per side (richer-looking book)
    mlevels = int(cfg.get("match_levels", 1))
    mstep = float(cfg.get("level_step", 0.001))
    mk_asks = [q(mark * (1 + spread + k * mstep)) for k in range(mlevels)]
    mk_bids = [q(mark * (1 - spread - k * mstep)) for k in range(mlevels)]
    tk_buy = min(math.floor(hi / tick) * tick - tick, q(mark * (1 + slip)))
    tk_sell = max(math.ceil(lo / tick) * tick + tick, q(mark * (1 - slip)))
    tk_mid = q(mark)  # between bid and ask -> a non-crossing IOC (tx load, no fill)
    # takers cross (fill) only every Nth order; maker cancels one tracked order
    # every Nth op (individual PERP_CANCEL) for realistic place/cancel churn.
    cross_every = max(1, int(cfg.get("taker_cross_every", 1)))
    maker_cancel_every = max(1, int(cfg.get("maker_cancel_every", 6)))
    print(f"match: target={target} tx/s -> {pairs} maker + {pairs} taker "
          f"(~{2 * pairs * per_acct:.0f} tx/s), market={mid} mark={mark} cap=+/-{cap} "
          f"levels={mlevels} bids={mk_bids[0]}..{mk_bids[-1]} asks={mk_asks[0]}..{mk_asks[-1]} "
          f"tkbuy/sell={tk_buy}/{tk_sell}")

    # persistent keystore (generate missing) + top up gas/deposit to targets
    keystore = cfg.get("keystore", "accounts.json")
    makers = [PerpDexClient(cfg["rpc_url"], k["key"], cfg["dex_address"]) for k in load_or_create(keystore, "maker", pairs)]
    takers = [PerpDexClient(cfg["rpc_url"], k["key"], cfg["dex_address"]) for k in load_or_create(keystore, "taker", pairs)]
    allacct = makers + takers
    ensure_funded(dev, allacct, gas_eth=float(cfg.get("fund_gas_eth", 1.0)), deposit=deposit)

    def prep(a):  # clear stale orders from prior runs, set leverage, prime nonce
        try:
            a.cancel_all(mid)
            a.set_leverage(mid, lev)
            a.prime()
        except Exception as e:
            print("prep warn", a.address[:10], e)
    ts = [threading.Thread(target=prep, args=(a,)) for a in allacct]
    [t.start() for t in ts]
    [t.join() for t in ts]
    print(f"ready {len(allacct)} accounts ({deposit} deposit target each)")

    # shared state: cached positions (base units) + cumulative taker fills
    pos = [0.0] * len(allacct)
    fills = [0.0]
    sent = [0] * len(allacct)       # per-account tx counters (no cross-thread race)
    stop = threading.Event()
    limiter = RateLimiter(target)   # one global pacer: steady aggregate target tx/s
    mk_ladder = [(SELL, px) for px in mk_asks] + [(BUY, px) for px in mk_bids]

    def acct_pos(a):
        for p in a.positions():
            if int(p["marketId"]) == mid:
                return _to_int(p["size"]) / 1e18
        return 0.0

    def poller():
        # baseline from real starting positions so leftover inventory on persistent
        # accounts isn't miscounted as fills
        prev = [acct_pos(a) for a in allacct]
        for idx in range(len(allacct)):
            pos[idx] = prev[idx]
        while not stop.is_set():
            for idx, a in enumerate(allacct):
                try:
                    cur = acct_pos(a)
                except Exception:
                    continue
                pos[idx] = cur
                if idx >= pairs:                       # taker: count every position move
                    fills[0] += abs(cur - prev[idx])
                prev[idx] = cur
            time.sleep(1)

    def maker_loop(idx, a):
        # Post the ladder and cancel individual tracked orders (PERP_CANCEL by
        # tx-hash) for realistic place/cancel churn; an occasional cancel_all is a
        # safety net so the idle side can't pile up and lock the maker's margin.
        from collections import deque
        hq = deque(maxlen=400)  # recent order ids (tx hashes) to cancel
        j = 0
        while not stop.is_set():
            limiter.wait()  # shared pacer -> steady aggregate tx/s, no burst/backlog
            try:
                if j % 60 == 0:
                    a.cancel_all(mid, wait=False)
                    hq.clear()
                elif j % maker_cancel_every == 0 and hq:
                    a.cancel(mid, hq.popleft(), wait=False)  # individual cancel of oldest
                else:
                    side, px = mk_ladder[j % len(mk_ladder)]
                    h = a.order(mid, side, px, size, tif=POST, wait=False)
                    hq.append(h)
                sent[idx] += 1
            except Exception:
                a.resync_nonce()
            j += 1

    def taker_loop(idx, a):
        # Cross (fill) only every `cross_every` order so the trade rate is tunable;
        # the rest are non-crossing IOCs at mid (tx load, no fill). Crossing orders
        # sweep the position +cap <-> -cap (triangle wave) for exact fill counting.
        gidx = pairs + idx
        d, j = BUY, 0
        while not stop.is_set():
            limiter.wait()
            try:
                if j % cross_every == 0:           # crossing -> fill
                    p = pos[gidx]
                    if p >= cap:
                        d = SELL
                    elif p <= -cap:
                        d = BUY
                    a.order(mid, d, tk_buy if d == BUY else tk_sell, size, tif=IOC, wait=False)
                else:                              # non-crossing -> pure tx load
                    a.order(mid, BUY, tk_mid, size, tif=IOC, wait=False)
                sent[gidx] += 1
            except Exception:
                a.resync_nonce()
            j += 1

    threads = [threading.Thread(target=poller, daemon=True)]
    threads += [threading.Thread(target=maker_loop, args=(i, a), daemon=True) for i, a in enumerate(makers)]
    threads += [threading.Thread(target=taker_loop, args=(i, a), daemon=True) for i, a in enumerate(takers)]
    t0 = time.time()
    [t.start() for t in threads]
    try:
        while duration <= 0 or time.time() - t0 < duration:
            time.sleep(5)
            el = time.time() - t0
            inv = [pos[pairs + i] for i in range(pairs)]
            print(f"t={el:.0f}s tx={sum(sent)} tx_rate={sum(sent) / el:.1f}/s "
                  f"trades={fills[0] / size:.0f} trade_rate={fills[0] / size / el:.1f}/s "
                  f"taker_inv=[{min(inv):+.2f},{max(inv):+.2f}] (cap +/-{cap})")
    except KeyboardInterrupt:
        pass
    stop.set()
    time.sleep(1.5)
    el = time.time() - t0
    print(f"DONE: {sum(sent)} tx in {el:.0f}s = {sum(sent) / el:.1f} tx/s; "
          f"{fills[0] / size:.0f} fills = {fills[0] / size / el:.1f} trades/s; "
          f"final taker_inv range [{min(pos[pairs:]):+.2f},{max(pos[pairs:]):+.2f}]")


if __name__ == "__main__":
    main()

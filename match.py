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

  python match.py [config.json] [target_trades_s] [duration_s]

duration 0 = run until Ctrl-C.
"""
import math
import sys
import threading
import time

from web3 import Web3

from dex import (PerpDexClient, BUY, SELL, POST, IOC, DEFAULT_BAND_BPS,
                 band_bounds, load_role_config, _to_int)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_role_config(path, "maker")
    target = float(sys.argv[2]) if len(sys.argv) > 2 else float(cfg.get("match_tps", 20))
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("match_duration", 0))
    per_acct = float(cfg.get("per_account_tps", 4))
    lev = int(cfg["leverage"])
    deposit = float(cfg.get("match_deposit", 50000))
    size = float(cfg.get("order_size", 0.01))
    cap = float(cfg.get("inventory_cap", 0.5))        # max |position| per account (base units)
    spread = float(cfg.get("spread", 0.0005))         # maker offset (< taker_slippage)
    slip = float(cfg.get("taker_slippage", 0.01))
    mid = cfg["market_id"]
    pairs = max(1, math.ceil(target / per_acct))

    dev = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    w3 = dev.w3
    mi = dev.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    mark = dev.mark_price(mid) or float(cfg["ref_price"])
    lo, hi = band_bounds(mark, band_bps)
    q = lambda x: round(x / tick) * tick
    mk_ask, mk_bid = q(mark * (1 + spread)), q(mark * (1 - spread))   # maker quotes
    tk_buy = min(math.floor(hi / tick) * tick - tick, q(mark * (1 + slip)))
    tk_sell = max(math.ceil(lo / tick) * tick + tick, q(mark * (1 - slip)))
    print(f"match: target={target} trades/s -> {pairs} maker + {pairs} taker, market={mid} "
          f"mark={mark} cap=+/-{cap} mkbid/ask={mk_bid}/{mk_ask} tkbuy/sell={tk_buy}/{tk_sell}")

    runid = str(int(time.time()))

    def newacct(role, i):
        return PerpDexClient(cfg["rpc_url"], Web3.keccak(text=f"match-{runid}-{role}-{i}").hex(), cfg["dex_address"])
    makers = [newacct("m", i) for i in range(pairs)]
    takers = [newacct("t", i) for i in range(pairs)]
    allacct = makers + takers

    # fund (sequential from dev): gas + spot token 2
    gp, cid = w3.eth.gas_price, w3.eth.chain_id
    for a in allacct:
        if w3.eth.get_balance(a.address) < w3.to_wei(0.5, "ether"):
            tx = {"to": a.address, "value": w3.to_wei(1, "ether"), "gas": 21000,
                  "nonce": w3.eth.get_transaction_count(dev.address), "chainId": cid, "gasPrice": gp}
            w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(dev.acct.sign_transaction(tx).raw_transaction))
        dev.token_transfer(a.address, deposit)

    def prep(a):
        try:
            a.deposit(deposit)
            a.set_leverage(mid, lev)
            a.prime()
        except Exception as e:
            print("prep warn", a.address[:10], e)
    ts = [threading.Thread(target=prep, args=(a,)) for a in allacct]
    [t.start() for t in ts]
    [t.join() for t in ts]
    print(f"funded+deposited {len(allacct)} accounts ({deposit} each)")

    # shared state: cached positions (base units) + cumulative taker fills
    pos = [0.0] * len(allacct)
    fills = [0.0]
    stop = threading.Event()
    dt = pairs / target

    def acct_pos(a):
        for p in a.positions():
            if int(p["marketId"]) == mid:
                return _to_int(p["size"]) / 1e18
        return 0.0

    def poller():
        prev = [0.0] * len(allacct)
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
        # always quote both sides. This is a closed system (maker position =
        # -taker position), so the takers reversing at ±cap keeps maker inventory
        # mirrored and bounded too. Throttling a maker side here instead starves
        # the takers (no bid to hit) and deadlocks mid-band.
        nxt = time.time()
        while not stop.is_set():
            try:
                a.order(mid, SELL, mk_ask, size, tif=POST, wait=False)
                a.order(mid, BUY, mk_bid, size, tif=POST, wait=False)
            except Exception:
                a.resync_nonce()
            nxt += dt
            sl = nxt - time.time()
            if sl > 0:
                time.sleep(sl)

    def taker_loop(idx, a):
        # hold a direction until hitting a cap, then reverse: position sweeps
        # +cap <-> -cap (triangle wave). Monotonic within each leg, so the 1s
        # |delta-position| poll counts every fill exactly, and margin stays bounded.
        gidx, nxt, d = pairs + idx, time.time(), BUY
        while not stop.is_set():
            p = pos[gidx]
            if p >= cap:
                d = SELL
            elif p <= -cap:
                d = BUY
            px = tk_buy if d == BUY else tk_sell
            try:
                a.order(mid, d, px, size, tif=IOC, wait=False)
            except Exception:
                a.resync_nonce()
            nxt += dt
            sl = nxt - time.time()
            if sl > 0:
                time.sleep(sl)

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
            print(f"t={el:.0f}s trades={fills[0] / size:.0f} rate={fills[0] / size / el:.1f}/s "
                  f"taker_inv=[{min(inv):+.2f},{max(inv):+.2f}] (cap +/-{cap})")
    except KeyboardInterrupt:
        pass
    stop.set()
    time.sleep(1.5)
    el = time.time() - t0
    print(f"DONE: {fills[0] / size:.0f} fills in {el:.0f}s = {fills[0] / size / el:.1f} trades/s; "
          f"final taker_inv range [{min(pos[pairs:]):+.2f},{max(pos[pairs:]):+.2f}]")


if __name__ == "__main__":
    main()

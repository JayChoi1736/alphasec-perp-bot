#!/usr/bin/env python3
"""Watch perp orderbook depth diffs + trades live over WebSocket (no polling).

Subscribes to the node's `perpTrades` feed (ws://<host>:8548 on a dev node) and
prints each depth-diff (`b`=bid levels, `a`=ask levels; qty 0 = level removed)
and trade event as it arrives.

  python watch.py [config.json]

Config: top-level `rpc_url` + `market_id`; optional `ws_url` (else derived as
ws://<rpc-host>:8548). Set market_id to 0 to watch all markets.
"""
import json
import sys
from datetime import datetime

from websocket import create_connection


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]  # millisecond precision


def ws_url_from(cfg):
    if cfg.get("ws_url"):
        return cfg["ws_url"]
    host = cfg["rpc_url"].split("://", 1)[-1].split(":", 1)[0].split("/", 1)[0]
    return f"ws://{host}:8548"


def main():
    cfg = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "config.json"))
    url = ws_url_from(cfg)
    want = str(cfg.get("market_id", 0))
    try:
        ws = create_connection(url)
    except Exception as e:
        sys.exit(f"cannot connect WS at {url}: {e}\n"
                 f"the node must be started with WS enabled, e.g. "
                 f"--ws --ws.addr=0.0.0.0 --ws.port=8548 --ws.api=eth,net,web3,arb")
    ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["perpTrades"]}))
    print(f"watching {url} market={want or 'ALL'} (sub {json.loads(ws.recv()).get('result')})")
    while True:
        msg = json.loads(ws.recv())
        if msg.get("method") != "eth_subscription":
            continue
        payload = msg["params"]["result"]
        for it in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(it, dict):
                continue
            d = it.get("data", it)
            mkt = str(d.get("s") or d.get("marketId") or d.get("symbol") or "")
            if want != "0" and mkt and mkt != want:
                continue
            b, a = d.get("b"), d.get("a")
            if b or a:  # depth diff (qty 0 = level removed)
                print(f"{ts()} [depth m{mkt}] bids={b or []} asks={a or []}")
            else:        # trade or other event
                print(f"{ts()} [event m{mkt}] {json.dumps(d, separators=(',', ':'))}")


if __name__ == "__main__":
    main()

"""Local sub-account keystore + pre-flight funding.

Sub-accounts are persisted in a JSON keystore (default accounts.json, git-ignored
— it holds private keys) and reused across runs. Each launch:
  load_or_create() ensures the group has N keys (generates+saves any missing),
  ensure_funded()  tops every account up to the gas/deposit targets before start.

Keystore shape: { "<group>": [ {"address","key"}, ... ] }
"""
import json
import os

from eth_account import Account

from dex import _to_int


def load_or_create(path, group, n):
    """Return n {address,key} dicts for `group`, generating + persisting any missing."""
    data = {}
    if os.path.exists(path):
        data = json.load(open(path))
    g = data.get(group, [])
    created = 0
    while len(g) < n:
        a = Account.create()
        k = a.key.hex()
        g.append({"address": a.address, "key": k if k.startswith("0x") else "0x" + k})
        created += 1
    data[group] = g
    json.dump(data, open(path, "w"), indent=2)
    if created:
        print(f"keystore {path}: generated {created} new '{group}' account(s) (total {len(g)})")
    return g[:n]


def ensure_funded(dev, subs, gas_eth=1.0, deposit=0.0, token="2"):
    """Top each sub-account up to targets before a run (the 'rebalance first' step).

    - gas: if ETH < gas_eth/2, send gas_eth from dev.
    - perp deposit: if walletBalance < deposit, transfer the shortfall as spot
      token from dev and deposit it. dev = funded signer (PerpDexClient).
    """
    w3 = dev.w3
    gp, cid = w3.eth.gas_price, w3.eth.chain_id
    gassed = topped = 0
    for a in subs:
        if w3.eth.get_balance(a.address) < w3.to_wei(gas_eth / 2, "ether"):
            tx = {"to": a.address, "value": w3.to_wei(gas_eth, "ether"), "gas": 21000,
                  "nonce": w3.eth.get_transaction_count(dev.address), "chainId": cid, "gasPrice": gp}
            w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(dev.acct.sign_transaction(tx).raw_transaction))
            gassed += 1
        if deposit > 0:
            wb = _to_int(a.perp_account().get("walletBalance")) / 1e18
            if wb < deposit:
                need = deposit - wb
                dev.token_transfer(a.address, need, token=token)
                a.deposit(need, token=token)
                topped += 1
    print(f"funding: gassed {gassed}, deposit-topped {topped} of {len(subs)} accounts "
          f"(targets: gas={gas_eth} ETH, deposit={deposit})")

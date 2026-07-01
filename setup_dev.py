#!/usr/bin/env python3
"""Dev-only: fund the taker account and ensure market 22 has an oracle price.

The maker uses the dev key (already funded); the taker uses a second key that
needs gas (ETH) + spot token 2 (to perp-deposit) + a registered oracle so
orders clear the price band. Idempotent enough to re-run.

  python setup_dev.py [config.dev.json]
"""
import sys

from lib.dex import PerpDexClient, load_role_config


def main():
    # The maker key in the dev config is the node's dev account (funded + an
    # OracleSubmitter), so it doubles as the setup signer — no key hardcoded here.
    path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.dev.json"
    common = load_role_config(path, "maker")
    taker = load_role_config(path, "taker")
    dev = PerpDexClient(common["rpc_url"], common["private_key"], common["dex_address"])
    taker_addr = PerpDexClient(common["rpc_url"], taker["private_key"], common["dex_address"]).address
    mid = common["market_id"]
    w3 = dev.w3

    # 0) grant the dev key the OracleSubmitter role (chain owner can; idempotent)
    if not dev.is_oracle_submitter():
        dev.add_oracle_submitter(dev.address)
        print("granted OracleSubmitter to dev key")

    # 1) ensure oracle price
    r = dev.oracle_price(mid, common["ref_price"])
    print(f"oracle market {mid} = {common['ref_price']} (status {r.status})")

    # 2) gas for the taker
    bal = w3.eth.get_balance(taker_addr) / 1e18
    if bal < 1:
        tx = {"to": taker_addr, "value": w3.to_wei(10, "ether"), "gas": 21000,
              "nonce": w3.eth.get_transaction_count(dev.address),
              "chainId": w3.eth.chain_id, "gasPrice": w3.eth.gas_price}
        h = w3.eth.send_raw_transaction(dev.acct.sign_transaction(tx).raw_transaction)
        w3.eth.wait_for_transaction_receipt(h)
        print(f"sent 10 ETH gas to taker {taker_addr}")
    else:
        print(f"taker {taker_addr} already has {bal:.2f} ETH")

    # 3) give the taker spot token 2 so it can perp-deposit
    before = dev._rpc("eth_getTokenBalances", [taker_addr, "latest"])
    dev.token_transfer(taker_addr, 20000, token="2")
    after = dev._rpc("eth_getTokenBalances", [taker_addr, "latest"])
    print(f"taker token balances before={before} after={after}")


if __name__ == "__main__":
    main()

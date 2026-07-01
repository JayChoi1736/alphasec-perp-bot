"""Wire-format self-check for the per-command encoding rules.

  python test_encode.py

PERP_ORDER price/quantity are HUMAN decimal strings; PERP_DEPOSIT amount is a
WEI integer string. Both verified against a live node — see dex.py header.
"""
from lib.dex import (encode_dex_input, dec_str, to_wei_str,
                 CMD_PERP_ORDER, CMD_PERP_DEPOSIT,
                 CMD_PERP_ORACLE_PRICE, CMD_PERP_ORACLE_PRICE_BATCH)

owner = "0x000000000000000000000000000000000000ab01"

# --- order: price/quantity as human decimal strings -----------------------
order = {"l1owner": owner, "marketId": 22, "side": 0,
         "price": dec_str(39960), "quantity": dec_str(0.01),
         "isReduceOnly": False, "timeInForce": 2}
order_json = ('{"l1owner":"0x000000000000000000000000000000000000ab01",'
              '"marketId":22,"side":0,"price":"39960","quantity":"0.01",'
              '"isReduceOnly":false,"timeInForce":2}')
assert encode_dex_input(CMD_PERP_ORDER, order) == \
    "0x" + format(CMD_PERP_ORDER, "02x") + order_json.encode("utf8").hex()
assert dec_str(39960) == "39960" and dec_str(0.01) == "0.01"

# --- deposit: amount as wei integer string (uint256, exact past 2^53) ------
dep = {"l1owner": owner, "token": "2", "amount": to_wei_str(5000)}
dep_json = ('{"l1owner":"0x000000000000000000000000000000000000ab01",'
            '"token":"2","amount":"5000000000000000000000"}')
assert encode_dex_input(CMD_PERP_DEPOSIT, dep) == \
    "0x" + format(CMD_PERP_DEPOSIT, "02x") + dep_json.encode("utf8").hex()
assert to_wei_str(5000) == "5000000000000000000000"

# --- oracle: index/cex as WEI decimal strings (1e18-scaled, spec §2.1) ------
orc = {"l1owner": owner, "marketId": 1,
       "indexPrice": to_wei_str(100), "cexPerpPrice": to_wei_str(101)}
orc_json = ('{"l1owner":"0x000000000000000000000000000000000000ab01",'
            '"marketId":1,"indexPrice":"100000000000000000000",'
            '"cexPerpPrice":"101000000000000000000"}')
assert encode_dex_input(CMD_PERP_ORACLE_PRICE, orc) == \
    "0x" + format(CMD_PERP_ORACLE_PRICE, "02x") + orc_json.encode("utf8").hex()
assert to_wei_str(100) == "100000000000000000000"  # matches confluence doc example

# --- oracle batch (0x4D): entries array ------------------------------------
batch = {"l1owner": owner, "entries": [
    {"marketId": 1, "indexPrice": to_wei_str(100), "cexPerpPrice": to_wei_str(101)},
    {"marketId": 2, "indexPrice": to_wei_str(200), "cexPerpPrice": to_wei_str(201)}]}
assert encode_dex_input(CMD_PERP_ORACLE_PRICE_BATCH, batch).startswith(
    "0x" + format(CMD_PERP_ORACLE_PRICE_BATCH, "02x"))

print("OK: order=human-decimal-string, deposit/oracle=wei-integer-string")

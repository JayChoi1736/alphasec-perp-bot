"""Wire-format self-check for the per-command encoding rules.

  python test_encode.py

PERP_ORDER price/quantity are HUMAN decimal strings; PERP_DEPOSIT amount is a
WEI integer string. Both verified against a live node — see dex.py header.
"""
from dex import encode_dex_input, dec_str, to_wei_str, CMD_PERP_ORDER, CMD_PERP_DEPOSIT

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

print("OK: order=human-decimal-string, deposit=wei-integer-string")

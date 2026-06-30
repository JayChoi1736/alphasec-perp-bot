# Perf Stage Summary

| Stage | Target | Workers | TPS | Fills | Taker Submit | Maker Submit | Taker ms | Taker sign ms | Wait ms | Errors | Log | Profile |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| wide_accounts | 300.0 | 1 | 37.2 | 1159 | 1033 | 113 | 6757.5 | 2.9 | 6644.2 | maker_error:TimeoutError=9, maker_error:nonce=9, poll_error:TimeoutError=276, taker_error:TimeoutError=69, taker_error:nonce=1 | /tmp/perf-stage-wide_accounts-20260630-091701.log |  |

# Perf Stage Summary

| Stage | Target | Workers | TPS | Fills | Taker Submit | Maker Submit | Taker ms | Taker sign ms | Wait ms | Errors | Log | Profile |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| final_poll | 300.0 | 1 | 39.1 | 823 | 595 | 233 | 7300.3 | 2.9 | 3874.1 | maker_seed_error:insufficient_margin=2, taker_error:TimeoutError=433, taker_error:nonce=31 | /tmp/perf-stage-final_poll-20260630-092443.log |  |

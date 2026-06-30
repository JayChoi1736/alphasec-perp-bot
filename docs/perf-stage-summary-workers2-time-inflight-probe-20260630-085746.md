# Perf Stage Summary

| Stage | Target | Workers | TPS | Fills | Taker Submit | Maker Submit | Taker ms | Taker sign ms | Wait ms | Errors | Log | Profile |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| final_poll | 300.0 | 2 | 34.8 | 1078 | 1064 | 497 | 2091.2 | 2.9 | 1586.6 |  | /tmp/perf-stage-final_poll-t300-20260630-085746-w*.log |  |
| final_poll | 360.0 | 2 | 35.1 | 1088 | 1034 | 527 | 2456.1 | 3.1 | 1685.7 | maker_error:insufficient_margin=23 | /tmp/perf-stage-final_poll-t360-20260630-085746-w*.log |  |

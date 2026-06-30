# Perf Stage Summary

| Stage | Target | Workers | TPS | Fills | Taker Submit | Maker Submit | Taker ms | Taker sign ms | Wait ms | Errors | Log | Profile |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| final_poll | 300.0 | 2 | 40.5 | 1256 | 1235 | 1219 | 875.8 | 2.5 | 908.5 |  | /tmp/perf-stage-final_poll-t300-20260630-084909-w*.log |  |
| final_poll | 360.0 | 2 | 41.4 | 1285 | 1248 | 1393 | 1039.2 | 2.8 | 1075.0 | maker_error:insufficient_margin=60 | /tmp/perf-stage-final_poll-t360-20260630-084909-w*.log |  |
| final_poll | 420.0 | 2 | 40.0 | 1241 | 1272 | 1200 | 1168.6 | 2.5 | 1212.2 | maker_error:insufficient_margin=41 | /tmp/perf-stage-final_poll-t420-20260630-084909-w*.log |  |

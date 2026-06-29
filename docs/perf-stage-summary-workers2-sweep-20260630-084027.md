# Perf Stage Summary

| Stage | Target | Workers | TPS | Fills | Taker Submit | Maker Submit | Taker ms | Taker sign ms | Wait ms | Errors | Log | Profile |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| final_poll | 240.0 | 2 | 39.8 | 1236 | 1206 | 1382 | 706.1 | 3.4 | 734.4 |  | /tmp/perf-stage-final_poll-t240-20260630-084028-w*.log |  |
| final_poll | 300.0 | 2 | 42.0 | 1302 | 1290 | 1259 | 848.2 | 2.7 | 878.1 |  | /tmp/perf-stage-final_poll-t300-20260630-084028-w*.log |  |
| final_poll | 360.0 | 2 | 42.6 | 1323 | 1305 | 1215 | 992.4 | 2.5 | 1033.5 | maker_error:insufficient_margin=53 | /tmp/perf-stage-final_poll-t360-20260630-084028-w*.log |  |
| final_poll | 420.0 | 2 | 42.3 | 1314 | 1290 | 1207 | 1170.8 | 2.6 | 1211.7 | maker_error:insufficient_margin=54 | /tmp/perf-stage-final_poll-t420-20260630-084028-w*.log |  |

# Perf Stage Summary

| Stage | Target | TPS | Fills | Taker Submit | Maker Submit | Taker ms | Taker sign ms | Wait ms | Log | Profile |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| baseline | 80.0 | 39.0 | 1209 | 1273 | 627 | 193.3 | 4.1 | 218.7 | /tmp/perf-stage-baseline-t80-20260630-070902.log | /tmp/perf-stage-baseline-t80-20260630-070902.pprof.pb.gz |
| maker_guard | 80.0 | 40.2 | 1247 | 1313 | 60 | 185.2 | 5.1 | 210.2 | /tmp/perf-stage-maker_guard-t80-20260630-070902.log |  |
| baseline | 120.0 | 48.1 | 1492 | 1512 | 1150 | 245.6 | 3.5 | 279.1 | /tmp/perf-stage-baseline-t120-20260630-070902.log | /tmp/perf-stage-baseline-t120-20260630-070902.pprof.pb.gz |
| maker_guard | 120.0 | 57.0 | 1768 | 1779 | 90 | 194.1 | 4.4 | 229.4 | /tmp/perf-stage-maker_guard-t120-20260630-070902.log |  |
| baseline | 160.0 | 51.5 | 1598 | 1625 | 1443 | 309.9 | 3.0 | 350.0 | /tmp/perf-stage-baseline-t160-20260630-070902.log | /tmp/perf-stage-baseline-t160-20260630-070902.pprof.pb.gz |
| maker_guard | 160.0 | 52.6 | 1630 | 1892 | 480 | 259.8 | 3.6 | 296.5 | /tmp/perf-stage-maker_guard-t160-20260630-070902.log |  |

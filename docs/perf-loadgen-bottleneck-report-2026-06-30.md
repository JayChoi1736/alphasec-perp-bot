# Perf Loadgen Bottleneck Report

Date: 2026-06-30 KST
Environment: `perf`
RPC path: `https://l2-rpc-perf.dexor.trade`
Scope: local `alphasec-perp-bot` load generator, perf core runtime config, and profiling evidence

## Verdict

`GOMAXPROCS=4` was a real perf-only config cap. Removing it and matching prod runtime env behavior raised the best observed filled IOC throughput from `74.0` to `146.7` trades/s.

After that config fix, the remaining ceiling is still core-side. The latest profile during post-change load is led by sequencer block creation and transaction sequencing. Local signing, DNS, backend whitelist/proxy, and simple account fanout are not the current primary bottlenecks.

## GitOps Change

Devops change pushed:

```text
repo: ../kaia-orderbook-dex-devops
commit: f905097 perf: match core runtime env with prod
file: charts/kaia-orderbook-dex-core/values-perf.yaml
change: removed perf-only GOMAXPROCS=4 and GOGC=100 overrides
```

ArgoCD/live verification:

```text
ArgoCD revision: f905097f3bdf0d18b18a86971d1fe9b004287f60
sync: Synced
health: Healthy
live pod env:
  DD_ENV=perf
  DD_SERVICE=kaia-orderbook-dex-core
  no GOMAXPROCS
  no GOGC
```

## Post-Change Measurements

Best post-change baseline with overlapping pprof:

```text
log: /tmp/perf-post-gomax-pprof-baseline-20260630-055553.log
profile: /tmp/core-cpu-post-gomax-20260630-055553.pb.gz
target: 300 trades/s
duration: 45s
result: 6756 fills in 46s = 146.7 trades/s
taker submit: 6849
maker submit: 4222
taker avg send latency: 182.7ms
maker avg send latency: 170.3ms
taker signing avg latency: 2.6ms
taker in-flight wait avg: 223.7ms
```

Staged retest summary:

```text
summary: docs/perf-stage-summary-after-gomaxprod-2026-06-30.md
baseline: 2815 fills in 21s = 134.0 trades/s
wide_accounts: 2105 fills in 21s = 99.3 trades/s
```

The earlier immediate post-change baseline also reached:

```text
log: /tmp/perf-stage-baseline-20260630-054748.log
result: 3038 fills in 21s = 144.3 trades/s
```

## Post-Change CPU Profile

Profile captured during the `146.7` trades/s run:

```text
profile: /tmp/core-cpu-post-gomax-20260630-055553.pb.gz
duration: 20.07s
total samples: 16.23s
```

Relevant `go tool pprof -top -cum -nodefraction=0` lines:

```text
7.43s 45.78% github.com/kaiachain/kaia-orderbook-dex-core/execution/gethexec.(*Sequencer).createBlock
7.27s 44.79% github.com/kaiachain/kaia-orderbook-dex-core/execution/gethexec.(*ExecutionEngine).sequenceTransactionsWithBlockMutex
2.57s 15.83% github.com/ethereum/go-ethereum/core/perp/v1/book.(*OrderBook).SnapshotDirtyTracking
2.57s 15.83% github.com/ethereum/go-ethereum/core/perp/v1/book.copyBoolMap
2.20s 13.56% github.com/ethereum/go-ethereum/internal/ethapi.SubmitTransaction
1.91s 11.77% runtime.mapassign_faststr
1.07s  6.59% aeshashbody
```

Interpretation:

```text
After removing the scheduler cap, dirty snapshot copy is less dominant than before,
but the largest cumulative path is still core sequencer block creation and transaction sequencing.
SubmitTransaction is visible now, but it remains below the sequencer path.
Local signing stays around 2.5-2.7ms and is not the throughput ceiling.
```

## Pre-Change CPU Profile

Before the runtime env change, the profile was more heavily dominated by dirty order snapshot copying:

```text
profile: /tmp/core-cpu-filled-20260630-042410.pb.gz
duration: 30.18s
total samples: 38.73s

29.85s 77.07% gethexec.(*Sequencer).createBlock
29.79s 76.92% ExecutionEngine.sequenceTransactionsWithBlockMutex
26.78s 69.15% book.(*OrderBook).SnapshotDirtyTracking
26.77s 69.12% book.copyBoolMap
13.02s 33.62% aeshashbody
 7.28s 18.80% runtime.mapassign_faststr
 1.43s  3.69% internal/ethapi.SubmitTransaction
```

Core source locations matching the profile:

```text
../kaia-orderbook-dex-core/go-ethereum/core/perp/v1/book/orderbook.go
- dirtyOrders/deletedOrders are string-keyed maps.
- SnapshotDirtyTracking copies those maps at snapshot/block boundaries.
- copyBoolMap creates a new map and assigns every string key.

../kaia-orderbook-dex-core/go-ethereum/core/perp/v1/dispatcher/perp_dispatcher_snapshot.go
- GetOrderDeltaAndReset collects dirty/deleted order state from engines and resets tracking.

../kaia-orderbook-dex-core/go-ethereum/core/perp/v1/engine/perp_symbol_engine_dirty.go
- dirtyOrderIDs/deletedOrderIDs are string-keyed sets.
```

## Exclusions

Local signing is not the bottleneck:

```text
post-change baseline signing: 2.6ms
wide_accounts signing: 2.5ms
```

Account fanout did not raise filled TPS:

```text
wide_accounts:
  makers=40
  takers=300
  MATCH_PER_ACCOUNT_TPS=1
  MATCH_ACCOUNT_INFLIGHT=1
  result=99.3 trades/s
  taker avg send latency=1311.6ms
  taker in-flight wait avg=1258.6ms
```

Backend whitelist/proxy is not on this test path:

```text
loadgen RPC: https://l2-rpc-perf.dexor.trade
devops mapping: perf-core-http-neg, port 8547
backend RPC proxy: l2-sequencer-perf.dexor.trade, port 8545
```

DNS is not supported by the profiles as the primary limiter. The hot samples are inside core execution, and the load generator can repeatedly reach 130-146 trades/s through the NEG host.

## Local Loadgen Changes

Relevant local bot changes:

```text
match.py
- async aiohttp JSON-RPC hot path
- shared Web3 setup session
- latency counters for send/sign/in-flight wait
- separate maker/taker account fanout controls
- nonce mode support
- cancellation-safe taker in-flight task cleanup

perf_stages.py
- staged baseline/wide runner
- markdown summary output
- stage timeout guard so a hung subprocess cannot block the whole run
- optional `--pprof-url` capture that starts after the load generator prints `ready`
- raw pprof profile path included in summary output
- optional `--target-sweep` to run the same stages across multiple target TPS values without log collisions

dex.py
- reusable signing path with cached chain metadata
- shared Web3 session support
```

## Current Max TPS

Current best observed filled IOC throughput:

```text
146.7 trades/s
```

This is about `1.98x` the previous best `74.0 trades/s` measured while perf had `GOMAXPROCS=4`.

## Next Bottleneck Work

Highest impact:

```text
1. Core transaction sequencing/block creation path.
2. Core dirty order snapshot/copy path, especially string-key map copy/hash/assignment.
3. SubmitTransaction/RPC admission path after the above is reduced.
```

Load generator work remaining:

```text
1. Add a maker-liquidity guard stage that avoids avoidable insufficient-margin churn.
2. Keep staged runs on direct NEG RPC unless explicitly testing backend proxy behavior.
3. Use `--target-sweep` for longer max-search runs after each core/config change.
```

## Verification

Commands run:

```text
helm template perf-kaia-orderbook-dex-core charts/kaia-orderbook-dex-core -f charts/kaia-orderbook-dex-core/values-perf.yaml
kubectl -n argocd get application perf-kaia-orderbook-dex-core
kubectl -n kaia-dex-perf exec perf-kaia-orderbook-dex-core-nitro-0 -- printenv
.venv/bin/python -m unittest test_match_helpers.py test_encode.py test_perf_stages.py -q
.venv/bin/python -m py_compile match.py dex.py accounts.py perf_stages.py test_match_helpers.py test_perf_stages.py
go tool pprof -top -cum -nodefraction=0 /tmp/core-cpu-post-gomax-20260630-055553.pb.gz
.venv/bin/python perf_stages.py --config config.perf.json --target 120 --duration 8 --stages baseline --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-stages baseline --pprof-seconds 3 --summary /tmp/perf-stage-summary-pprof-smoke-20260630.md --stage-timeout 120
go tool pprof -top -cum -nodefraction=0 /tmp/perf-stage-baseline-20260630-060150.pprof.pb.gz
.venv/bin/python perf_stages.py --config config.perf.json --target-sweep 80,120 --duration 5 --stages baseline --summary /tmp/perf-stage-summary-target-sweep-smoke-20260630.md --stage-timeout 120
```

No core code changes were made.

## Retest After Local Docs Commit

Push status:

```text
git push origin main
ERROR: Permission to JayChoi1736/alphasec-perp-bot.git denied to probepark.
viewerPermission: READ
```

Retest command:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target 360 --duration 45 --stages baseline,maker_guard --summary docs/perf-stage-summary-retest-2026-06-30.md --stage-timeout 240 --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-seconds 20 --pprof-stages baseline,maker_guard
```

Latest clean retest:

```text
summary: docs/perf-stage-summary-retest-2026-06-30.md
baseline: 6144 fills in 46s = 133.5 trades/s
maker_guard: 5193 fills in 46s = 112.8 trades/s
```

Latest pprof evidence:

```text
baseline profile: /tmp/perf-stage-baseline-20260630-061402.pprof.pb.gz
Duration: 20.17s, Total samples = 19.20s
Sequencer.createBlock: 10.09s / 52.55% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 10.01s / 52.14% cum
OrderBook.SnapshotDirtyTracking: 5.68s / 29.58% cum
SubmitTransaction: 1.91s / 9.95% cum

maker_guard profile: /tmp/perf-stage-maker_guard-20260630-061402.pprof.pb.gz
Duration: 20.13s, Total samples = 18.95s
Sequencer.createBlock: 11.18s / 59.00% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 11.06s / 58.36% cum
OrderBook.SnapshotDirtyTracking: 8.57s / 45.22% cum
SubmitTransaction: 1.47s / 7.76% cum
```

Current clean max remains `146.7 trades/s`. Experimental `time_inflight2` reached `150.2 trades/s`, but it produced two taker nonce errors, so it is not counted as the clean ceiling.

Additional local loadgen experiments are recorded in `docs/perf-loadgen-experiment-log-2026-06-30.md`. The tested variants did not beat the current clean max; the core-side bottleneck diagnosis remains unchanged.

Latest loadgen-side code change:

```text
match.py: poll taker positions only; maker positions are no longer polled because they are not used for fill counting or taker inventory control.
test: test_position_poll_items_skip_maker_accounts
```

Latest profile after that change:

```text
summary: docs/perf-stage-summary-taker-only-poll-2026-06-30.md
result: 3470 fills in 46s = 75.3 trades/s
profile: /tmp/perf-stage-baseline-20260630-064418.pprof.pb.gz
Sequencer.createBlock: 27.82s / 58.88% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 27.61s / 58.43% cum
OrderBook.SnapshotDirtyTracking: 19.64s / 41.57% cum
SubmitTransaction: 3.79s / 8.02% cum
```

This run did not change the clean max because maker insufficient-margin errors dominated the test. The code change only removes unnecessary local RPC load.

## Current Retest State

Later retests are no longer reproducing the `146.7 trades/s` clean max. The strongest immediate cause is test account state, not local signing or DNS:

```text
MATCH_MAKER_CANCEL_EVERY=1: 1966 fills in 31s = 63.3 trades/s, margin errors removed but cancel traffic dominated
MATCH_LEVELS=1: 2359 fills in 31s = 76.1 trades/s
MATCH_INVENTORY_CAP=0.01: 3290 fills in 46s = 71.4 trades/s, maker insufficient-margin errors persisted
owner L2 ETH: ~0.03 after low-gas fresh-account smoke tests, not enough for another broad fresh-account sweep with MATCH_GAS_ETH=0.1
fresh low-gas smoke: MATCH_GAS_ETH=0.0005 works for account creation/funding, but measured only 11.4-29.2 trades/s in tested combinations
fresh runner smoke: perf_stages.py --fresh-keystore-dir works end-to-end, measured 27.1 trades/s with low-gas env overrides
```

Interpretation: the earlier clean ceiling remains the best measured max, but the current account set is not clean enough for a fair max search. A next clean run should fund the owner for fresh sub-accounts and then use the new `--fresh-keystore-dir` / `--env` runner options with a better maker sizing/depth strategy before comparing TPS again.

## Retest After Fresh Runner Commit

```text
summary: docs/perf-stage-summary-retest-20260630-070902.md
local commit: 46edd1c perf: add fresh keystore stage runner options
push status: git push origin main blocked, viewerPermission=READ on JayChoi1736/alphasec-perp-bot
rpc path: https://l2-rpc-perf.dexor.trade
owner L2 ETH before retest: 0.03
best retest result: maker_guard target=120, 1768 fills in 31s = 57.0 trades/s
baseline target=80: 39.0 TPS, maker insufficient-margin=50
baseline target=120: 48.1 TPS, maker insufficient-margin=51
baseline target=160: 51.5 TPS, maker insufficient-margin=83
maker_guard target=80: 40.2 TPS, maker_submit_ok=60
maker_guard target=120: 57.0 TPS, maker_submit_ok=90
maker_guard target=160: 52.6 TPS, maker_submit_ok=480
```

Latest profile evidence from the retest:

```text
t80 baseline: Sequencer.createBlock 67.61% cum, SequenceTransactions 67.16% cum, SnapshotDirtyTracking/copyBoolMap 56.32% cum, SubmitTransaction 3.46% cum
t120 baseline: Sequencer.createBlock 67.68% cum, SequenceTransactions 67.27% cum, SnapshotDirtyTracking/copyBoolMap 54.05% cum, SubmitTransaction 3.90% cum
t160 baseline: Sequencer.createBlock 66.39% cum, SequenceTransactions 66.01% cum, SnapshotDirtyTracking/copyBoolMap 54.76% cum, SubmitTransaction 4.30% cum
```

Current diagnosis:

```text
The latest retest does not replace the clean max of 146.7 TPS. Current account state is the immediate test blocker: baseline still emits maker insufficient-margin, while maker_guard avoids the error by reducing maker submissions and therefore cannot prove a higher clean ceiling. Core profile remains consistent with the earlier diagnosis: sequencer transaction sequencing and dirty order snapshot copy/hash work dominate CPU. No core code changes were made.
```

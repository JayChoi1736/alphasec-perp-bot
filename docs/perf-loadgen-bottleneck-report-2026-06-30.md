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

## Wide Recheck And Maker Adaptive Experiment

```text
wide recheck summary: docs/perf-stage-summary-wide-recheck-20260630-071917.md
wide_accounts target=240: 2021 fills in 31s = 65.1 TPS
wide taker avg latency: 3459.2ms
wide taker in-flight wait avg: 3356.1ms
wide maker insufficient-margin: 12
```

Result: wider fanout currently worsens RPC/core admission latency and does not recover the earlier max.

Optional loadgen change:

```text
match.py: per-maker order-size backoff after maker insufficient-margin
perf_stages.py: maker_adaptive stage
test coverage: maker_size_after_error helper and maker/taker loop call arity checks
```

Adaptive result:

```text
summary: docs/perf-stage-summary-maker-adaptive-20260630-072656.md
baseline target=240: 1662 fills in 31s = 53.6 TPS, maker insufficient-margin=154
maker_adaptive target=240: 1671 fills in 31s = 53.9 TPS, maker insufficient-margin=84
```

Current diagnosis remains:

```text
maker_adaptive reduces failed maker traffic, but it does not move max TPS. Current ceiling evidence still points to core transaction sequencing and order-book snapshot/copy work under the present perf/account state. The latest measured clean max remains 146.7 TPS.
```

## Final-Only Position Poll Experiment

Optional loadgen change:

```text
match.py: MATCH_POSITION_POLL_MODE=continuous|final|off
perf_stages.py: final_poll stage
```

Reason:

```text
Continuous position polling is accurate, but it adds read RPC pressure during the load window. final_poll keeps initial position reads, skips continuous position reads during load, then performs one final position poll after load cleanup. TPS denominator is frozen before the final read, so measurement read time is not mixed into load time.
```

Results:

```text
summary: docs/perf-stage-summary-final-poll-20260630-073400.md
baseline target=240: 1910 fills in 31s = 61.6 TPS, taker avg latency=418.8ms, wait=456.1ms
final_poll target=240: 2190 fills in 31s = 70.6 TPS, taker avg latency=365.7ms, wait=399.1ms

summary: docs/perf-stage-summary-final-poll-sweep-20260630-073539.md
final_poll target=300: 2150 fills in 31s = 69.3 TPS, taker avg latency=487.1ms, wait=520.7ms
final_poll target=360: 2069 fills in 31s = 66.6 TPS, taker avg latency=613.4ms, wait=649.2ms
```

Profile evidence:

```text
target=300: Sequencer.createBlock 70.42% cum, SequenceTransactions 69.98% cum, SnapshotDirtyTracking/copyBoolMap 60.27% cum, SubmitTransaction 4.84% cum
target=360: Sequencer.createBlock 70.11% cum, SequenceTransactions 69.50% cum, SnapshotDirtyTracking/copyBoolMap 58.54% cum, SubmitTransaction 4.65% cum
```

Current diagnosis:

```text
final_poll improves the current degraded run from 61.6 to 70.6 TPS at target=240 by reducing read pressure, but higher targets still fall as core sequencing latency and maker margin failures rise. It is a useful max-load stage, not a new clean max. Clean max remains 146.7 TPS from the earlier post-GOMAXPROCS run.
```

## Follow-up Current-State Retests

Retests after `final_poll` tried to isolate whether the lower current TPS was
caused by polluted low-index accounts, maker traffic, broad account fanout,
maker order size, or per-account in-flight settings.

```text
slice60 final_poll target=240: 57.6 TPS
taker-only final_poll target=240: 55.5 TPS
fanout60 final_poll target=240: 46.1 TPS
maker-size-0.0025 final_poll target=240: 50.5 TPS
inflight2 final_poll target=240: 51.5 TPS
local sweep final_poll target=180: 46.5 TPS
local sweep final_poll target=220: 52.8 TPS
local sweep final_poll target=260: 54.1 TPS
```

Current conclusion:

```text
These variants did not beat the 70.6 TPS current-state final_poll result and
do not replace the 146.7 TPS clean max. The local generator is no longer the
dominant proven bottleneck: signing remains low-millisecond, taker-only traffic
lowers fills, wider fanout increases latency, and in-flight=2 is not beneficial
on the clean path. The bottleneck evidence remains core sequencing plus dirty
snapshot copy/hash work, while maker insufficient-margin churn is the immediate
reason current retests underperform the clean post-GOMAXPROCS run.
```

## Latest Retest After Push Attempt

Push remained blocked by repository permissions:

```text
git push origin main
ERROR: Permission to JayChoi1736/alphasec-perp-bot.git denied to probepark.
```

The requested retest was still executed locally:

```text
summary: docs/perf-stage-summary-final-poll-retest-20260630-075002.md
profile: /tmp/perf-stage-final_poll-20260630-075002.pprof.pb.gz
stage: final_poll
target: 240 TPS
duration: 30s
result: 1470 fills in 31s = 47.4 TPS
taker_submit_ok: 1440
maker_submit_ok: 1377
maker insufficient-margin: 128
taker avg latency: 577.9ms
taker signing avg latency: 2.5ms
taker in-flight wait avg: 608.5ms
```

Profile evidence:

```text
Sequencer.createBlock: 19.02s / 75.75% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.97s / 75.55% cum
OrderBook.SnapshotDirtyTracking: 16.25s / 64.72% cum
book.copyBoolMap: 16.24s / 64.68% cum
SubmitTransaction: 0.70s / 2.79% cum
```

Conclusion:

```text
Latest retest confirms the same diagnosis. Local signing is not the limiter,
SubmitTransaction/RPC admission is visible but small in CPU, and the dominant
cost is core block sequencing plus dirty order snapshot copy/hash work. Current
accounts are degraded by maker insufficient-margin churn, so 47.4 TPS does not
replace the clean 146.7 TPS max.
```

## Maker Cooldown Rejection

An optional maker error cooldown was added to test whether repeated
insufficient-margin makers were wasting enough submit budget to become the
current bottleneck.

```text
code: MATCH_MAKER_ERROR_COOLDOWN_THRESHOLD, MATCH_MAKER_ERROR_COOLDOWN_SEC
stage: maker_cooldown
default behavior: unchanged unless the env vars are set
```

Measured results:

```text
summary: docs/perf-stage-summary-maker-cooldown-20260630-075610.md
final_poll target=240: 54.0 TPS
maker_cooldown threshold=2 target=240: 50.2 TPS

summary: docs/perf-stage-summary-maker-cooldown-aggressive-20260630-075825.md
maker_cooldown threshold=1 target=240: 50.8 TPS

summary: docs/perf-stage-summary-maker-cooldown-counters-20260630-080137.md
maker_cooldown threshold=1 target=240: 50.1 TPS
maker_cooldown=7
maker_cooldown_skipped=4
maker insufficient-margin=7
```

Profile from the latest cooldown run:

```text
Sequencer.createBlock: 19.01s / 75.47% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.95s / 75.23% cum
OrderBook.SnapshotDirtyTracking: 16.11s / 63.95% cum
book.copyBoolMap: 16.11s / 63.95% cum
SubmitTransaction: 0.78s / 3.10% cum
```

Conclusion:

```text
Maker cooldown reduces insufficient-margin errors but lowers filled TPS versus
final_poll. It is rejected as the max-TPS path. This narrows the diagnosis:
failed maker submits are not the primary throughput ceiling; core sequencing and
dirty snapshot copy/hash work remain the dominant bottleneck.
```

## Prep-Failed Account Filter Rejection

The repeated setup warning was traced to DEX command `0x45`, which is
`CMD_PERP_SET_LEVERAGE`. A loadgen-only filter was added so a stage can exclude
accounts that fail prep:

```text
code: MATCH_SKIP_PREP_FAILED=1
stage: healthy_accounts
default behavior: unchanged unless the env var is set
```

Measured result:

```text
summary: docs/perf-stage-summary-healthy-accounts-20260630-080727.md
final_poll target=240: 47.1 TPS
healthy_accounts target=240: 46.6 TPS
prep_skipped=4
filtered accounts: makers=4, takers=0
```

Profile from `healthy_accounts`:

```text
Sequencer.createBlock: 18.55s / 75.84% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.49s / 75.59% cum
OrderBook.SnapshotDirtyTracking: 15.88s / 64.92% cum
book.copyBoolMap: 15.88s / 64.92% cum
SubmitTransaction: 0.83s / 3.39% cum
```

Conclusion:

```text
Filtering prep-failed makers is useful diagnostics and keeps dirty accounts out
of a run, but it did not improve max TPS. The dominant bottleneck remains core
sequencing plus dirty snapshot copy/hash work.
```

## Healthy Maker Pool Rejection

The first 30 makers are heavily polluted with residual positions, so the
loadgen now has an optional maker pool selector:

```text
code: MATCH_MAKER_POOL_COUNT, MATCH_HEALTHY_MAKER_MIN_FREE, MATCH_HEALTHY_MAKER_MAX_ABS_POS
stage: healthy_makers
default behavior: unchanged unless the env vars are set
```

Measured result:

```text
summary: docs/perf-stage-summary-healthy-makers-20260630-081630.md
final_poll target=240: 43.4 TPS
healthy_makers target=240: 33.8 TPS
selected makers: 30/150
health_maker_skipped=120
healthy_makers errors={}
```

Profile from `healthy_makers`:

```text
Sequencer.createBlock: 19.30s / 77.11% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.24s / 76.87% cum
OrderBook.SnapshotDirtyTracking: 17.46s / 69.76% cum
book.copyBoolMap: 17.46s / 69.76% cum
SubmitTransaction: 0.61s / 2.44% cum
```

Conclusion:

```text
Healthy maker selection is useful for proving that margin errors are not the
primary throughput ceiling. It removes errors but lowers filled TPS and keeps
the profile dominated by core sequencing plus dirty snapshot copy/hash work.
```

## Push-Blocked Retest Update

Push did not update GitHub because the current account has read-only access:

```text
ERROR: Permission to JayChoi1736/alphasec-perp-bot.git denied to probepark.
```

Retest results from local `main`:

```text
summary: docs/perf-stage-summary-push-retest-20260630-082107.md
target=240 final_poll: 36.6 TPS, errors={}, taker avg=758.0ms, taker sign avg=3.2ms, wait avg=792.4ms

summary: docs/perf-stage-summary-push-retest-t300-20260630-082235.md
target=300 final_poll: 36.7 TPS, errors={}, taker avg=972.1ms, taker sign avg=2.6ms, wait avg=1009.0ms
```

Profile from target=240:

```text
Sequencer.createBlock: 19.18s / 76.51% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.12s / 76.27% cum
OrderBook.SnapshotDirtyTracking: 17.01s / 67.85% cum
book.copyBoolMap: 17.01s / 67.85% cum
SubmitTransaction: 0.72s / 2.87% cum
```

Updated diagnosis:

```text
Current clean final_poll throughput is 36-37 TPS and no longer rises when
target increases from 240 to 300. The historical clean max remains 146.7 TPS.
This retest does not change the bottleneck call: local signing is still only
~3ms, RPC admission/SubmitTransaction is small in CPU profile, and the dominant
CPU path is core sequencer block creation plus dirty order snapshot copy/hash
work.
```

## Multi-Worker Runner Update

New loadgen evidence:

```text
code: perf_stages.py --workers, accounts.py atomic/no-op keystore persistence
summary: docs/perf-stage-summary-workers-fixed-20260630-083515.md
workers=2 target=300 final_poll: 41.6 TPS, taker avg=855.2ms, sign avg=2.5ms, wait avg=885.6ms

summary: docs/perf-stage-summary-workers4-fixed-20260630-083617.md
workers=4 target=300 final_poll: 38.1 TPS, taker avg=956.6ms, sign avg=3.3ms, wait avg=976.7ms
```

Root cause fixed in loadgen:

```text
Concurrent stage workers could race on accounts.perf.json because
load_or_create() rewrote the keystore even when no account was generated.
This caused a worker JSONDecodeError and a partial/misleading aggregate run.
```

Updated diagnosis:

```text
Multi-worker fanout raises the current degraded-state TPS from 36.7 to 41.6,
but scaling stops by workers=4. This is a loadgen robustness improvement, not
a new ceiling. The remaining limiter is still core-side sequencing/snapshot
work, with local signing staying around 2.5-3.3ms.
```

## Workers=2 Clean Ceiling Update

Latest staged sweep:

```text
summary: docs/perf-stage-summary-workers2-sweep-20260630-084027.md
workers=2 target=240: 39.8 TPS, errors={}
workers=2 target=300: 42.0 TPS, errors={}
workers=2 target=360: 42.6 TPS, maker_error:insufficient_margin=53
workers=2 target=420: 42.3 TPS, maker_error:insufficient_margin=54
```

Clean profile run:

```text
summary: docs/perf-stage-summary-workers2-clean-pprof-20260630-084342.md
workers=2 target=300: 40.9 TPS, errors={}
taker avg=877.3ms, sign avg=3.2ms, wait avg=905.5ms
profile: /tmp/perf-stage-final_poll-20260630-084342.pprof.pb.gz
```

Profile:

```text
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.65s / 75.75% cum
OrderBook.SnapshotDirtyTracking: 17.88s / 68.93% cum
book.copyBoolMap: 17.88s / 68.93% cum
SendRawTransaction: 0.65s / 2.51% cum
SubmitTransaction: 0.58s / 2.24% cum
PublishTransaction: 0.58s / 2.24% cum
```

Updated bottleneck call:

```text
Current degraded-state clean max is workers=2 target=300 at 42.0 TPS. The
slightly higher 360/420 target results are dirty because maker insufficient
margin appears. Increasing target mostly increases wait and error pressure.
The clean profile still points to core sequencing and order snapshot copy/hash
work as the primary bottleneck; local signing and RPC admission are secondary.
```

## Rejected Loadgen Mitigations After Clean Ceiling

Additional workers=2 probes after the 42.0 TPS clean ceiling:

```text
maker backoff target=360: 39.3 TPS, maker_error:insufficient_margin=42, maker_size_backoff=10 on worker 1
relaxed healthy maker pool target=360: 39.0 TPS, maker_error:insufficient_margin=30
account fanout target=300 with MATCH_PER_ACCOUNT_TPS=6: 38.5 TPS, maker_error:insufficient_margin=55
time nonce + account_inflight=2 target=300: 34.8 TPS, errors={}, taker avg=2091.2ms
```

Updated bottleneck call:

```text
The load generator can now make these failure modes visible, but none moves the
clean ceiling above workers=2 target=300. Margin-pressure mitigations reduce
throughput or still leave maker insufficient-margin. Time nonce avoids nonce
errors in a small probe, but pushes latency above 2s and cuts maker submit
throughput. The limiting path remains core sequencing/snapshot work plus
current perf account state, not local signing or DNS.

## 09:09 KST Current Retest Update

Live deployment state before the retest:

```text
ArgoCD revision: f905097f3bdf0d18b18a86971d1fe9b004287f60
ArgoCD status: Synced / Healthy
core replicas: 1
core image: dev tag digest c20cfa320a46e6386e1823a610f90809a5796daf4be5f6cff7758688e5c127d8
core resources: requests cpu=8 memory=32Gi, limits cpu=15 memory=60Gi
live env: no GOMAXPROCS, no GOGC
```

Latest retest:

```text
summary: docs/perf-stage-summary-workers2-retest-20260630-090350.md
workers=2 target=300 with pprof: 37.9 TPS, errors={}
profile: /tmp/perf-stage-final_poll-20260630-090350.pprof.pb.gz

summary: docs/perf-stage-summary-workers2-clean-retest-20260630-090913.md
workers=2 target=300 no pprof: 35.4 TPS, errors={}

summary: docs/perf-stage-summary-workers2-t360-retest-20260630-090818.md
workers=2 target=360 no pprof: 36.1 TPS, maker_error:insufficient_margin=54

summary: docs/perf-stage-summary-workers2-healthy-retest-20260630-090717.md
healthy maker filter target=300: 33.6 TPS, errors={}
```

Current profile evidence:

```text
Sequencer.createBlock: 19.58s / 77.06% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.55s / 76.94% cum
OrderBook.SnapshotDirtyTracking: 17.90s / 70.44% cum
book.copyBoolMap: 17.90s / 70.44% cum
SendRawTransaction: 0.69s / 2.72% cum
SubmitTransaction: 0.64s / 2.52% cum
PublishTransaction: 0.63s / 2.48% cum
```

Current maker-account state:

```text
maker accounts queried: 150/150
free margin p50=1593.182, p90=1852.859, max=2104.034
abs position p50=0.029, p90=0.142, max=0.372
free>=1000 and abs_pos<=0.10: 71 accounts
```

Current bottleneck call:

```text
Historical best after removing the perf-only GOMAXPROCS cap remains 146.7 TPS, but the current live retest ceiling is 35-38 clean TPS. The current pprof is again dominated by core sequencing and dirty order snapshot copy/hash. target=360 is dirty because maker insufficient-margin errors return. Healthy maker filtering does not improve TPS, so account state is a secondary limiter for high-target runs, not the current clean target=300 ceiling. Loadgen-side signing, DNS, and RPC admission are not primary: signing is ~3ms and SubmitTransaction/PublishTransaction stay around 2.5% cumulative CPU.
```

## 09:41 KST Loadgen-Side Exclusions

Additional loadgen probes after preparing 600 taker accounts:

```text
maker once with 38 takers:
37.6 TPS, errors={}

wide_accounts with 300 takers and continuous polling:
37.2 TPS, dirty with poll/taker TimeoutError and nonce errors

300 takers, final_poll, maker refresh:
36.1 TPS, dirty with maker insufficient-margin=3

300 takers, maker once:
38.5 TPS, dirty with maker_seed insufficient-margin=2

300 takers, maker once, time nonce, account_inflight=2:
39.1 TPS, dirty with taker TimeoutError=433 and nonce=31

300 takers, maker once, time nonce, account_inflight=2, MATCH_RPC_TIMEOUT=30:
33.3 TPS, taker TimeoutError removed, still not a higher max

600 takers, maker once:
35.4 TPS, dirty with maker_seed insufficient-margin=2

300 takers, maker once, normal nonce, account_inflight=2:
37.5 TPS, dirty with taker nonce=349

600 takers, healthy maker pool, maker once, normal nonce, account_inflight=1:
37.1 TPS, errors={}, taker avg=9593.3ms, wait avg=8895.1ms
```

Updated bottleneck call:

```text
The local generator can now use 600 prepared taker accounts, so the earlier 38-account local ceiling is ruled out. maker_mode=once rules out maker refresh/cancel churn as the direct TPS ceiling. account_inflight=2 and time nonce can create more local pressure but make the run dirty and do not raise clean filled TPS. Increasing RPC timeout removes timeout symptoms but not the throughput ceiling. Current clean maximum remains about 37-38 TPS, with the remaining bottleneck on core/RPC response latency under sequencer and dirty snapshot work.
```

## 09:47 KST High-Fanout Profile Confirmation

Clean high-fanout profile:

```text
summary: docs/perf-stage-summary-takers600-healthy-once-pprof-20260630-094305.md
profile: /tmp/perf-stage-final_poll-20260630-094305.pprof.pb.gz
600 takers, healthy maker pool, maker once, normal nonce, account_inflight=1
result: 37.1 TPS, errors={}
taker avg=9724.5ms, wait avg=9027.2ms
```

Profile:

```text
Sequencer.createBlock: 12.95s / 76.54% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 12.93s / 76.42% cum
OrderBook.SnapshotDirtyTracking: 12.13s / 71.69% cum
book.copyBoolMap: 12.13s / 71.69% cum
SendRawTransaction: 0.27s / 1.60% cum
SubmitTransaction: 0.27s / 1.60% cum
PublishTransaction: 0.26s / 1.54% cum
```

Final current diagnosis:

```text
Current perf-state clean TPS is 37.1-37.9. The 600-taker clean profile confirms the same core-side bottleneck after ruling out local taker count, maker refresh churn, DNS caching, and RPC timeout as primary causes. The next TPS increase requires reducing core sequencer/snapshot cost or resetting/reducing accumulated dirty order state; loadgen-only changes have not produced a higher clean ceiling.
```

#!/usr/bin/env python3
"""Async matched load generator for real perp fills.

The hot path uses aiohttp JSON-RPC instead of one Web3 HTTPProvider per thread.
Web3 is still used for setup, funding, signing metadata, and custom read RPCs.

Usage:
    python match.py [config.json] [target_trades_s] [duration_s]

duration 0 = run until Ctrl-C.
"""

import asyncio
import math
import os
import socket
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

import aiohttp
import requests
from requests.adapters import HTTPAdapter

from accounts import ensure_funded, load_or_create
from dex import (
    BUY,
    CMD_PERP_CANCEL_ALL,
    CMD_PERP_ORDER,
    DEFAULT_BAND_BPS,
    IOC,
    POST,
    SELL,
    PerpDexClient,
    _to_int,
    band_bounds,
    dec_str,
    load_role_config,
)


def env_float(name, default):
    return float(os.environ.get(name, default))


def env_int(name, default):
    return int(os.environ.get(name, default))


def mode_enabled(value):
    return str(value).lower() not in ("0", "false", "no", "off")


def normalize_position_poll_mode(value):
    mode = str(value).strip().lower()
    if mode in ("1", "true", "yes", "on", "continuous"):
        return "continuous"
    if mode in ("final", "end", "once"):
        return "final"
    if mode in ("0", "false", "no", "off", "none"):
        return "off"
    raise ValueError("MATCH_POSITION_POLL_MODE must be continuous, final, or off")


def active_role_names(maker_mode, taker_mode):
    roles = []
    if maker_mode != "off":
        roles.append("maker")
    if mode_enabled(taker_mode):
        roles.append("taker")
    return tuple(roles)


def stagger_delay(index, total, interval):
    if total <= 1 or interval <= 0:
        return 0.0
    return interval * (index / total)


def worker_bounds(total, worker_index, worker_count):
    if worker_count <= 0:
        raise ValueError("worker_count must be positive")
    if worker_index < 0 or worker_index >= worker_count:
        raise ValueError("worker_index must be in [0, worker_count)")
    base, extra = divmod(total, worker_count)
    start = worker_index * base + min(worker_index, extra)
    end = start + base + (1 if worker_index < extra else 0)
    return start, end


def account_pair_count(target, per_account_tps, override=None):
    if override is not None:
        pairs = int(override)
        if pairs <= 0:
            raise ValueError("MATCH_TOTAL_PAIRS must be positive")
        return pairs
    if per_account_tps <= 0:
        raise ValueError("MATCH_PER_ACCOUNT_TPS must be positive")
    return max(1, math.ceil(target / per_account_tps))


def role_account_counts(target, per_account_tps, total_override=None, maker_override=None, taker_override=None):
    default_count = account_pair_count(target, per_account_tps, total_override)
    maker_count = (
        account_pair_count(target, per_account_tps, maker_override)
        if maker_override is not None
        else default_count
    )
    taker_count = (
        account_pair_count(target, per_account_tps, taker_override)
        if taker_override is not None
        else default_count
    )
    return maker_count, taker_count


def role_deposit_targets(default_deposit, maker_override=None, taker_override=None):
    maker_deposit = float(maker_override) if maker_override is not None else float(default_deposit)
    taker_deposit = float(taker_override) if taker_override is not None else float(default_deposit)
    return maker_deposit, taker_deposit


def position_poll_items(accounts, taker_offset):
    return list(enumerate(accounts[taker_offset:], start=taker_offset))


def filter_prepped_accounts(accounts, prep_results, skip_failed):
    if not skip_failed:
        return list(accounts), 0
    kept = []
    skipped = 0
    for account, result in zip(accounts, prep_results):
        if result is True:
            kept.append(account)
        else:
            skipped += 1
    skipped += max(0, len(accounts) - len(prep_results))
    return kept, skipped


def select_healthy_accounts(accounts, health_results, limit, min_free, max_abs_pos):
    candidates = []
    for account, health in zip(accounts, health_results):
        if isinstance(health, Exception):
            continue
        free = float(health.get("free", 0.0))
        position = float(health.get("position", 0.0))
        abs_pos = abs(position)
        if free < min_free or abs_pos > max_abs_pos:
            continue
        candidates.append((abs_pos, -free, account))
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = [account for _, _, account in candidates[:limit]]
    skipped = len(accounts) - len(selected)
    return selected, skipped


def choose_direction(position, cap, previous_direction):
    if position >= cap:
        return SELL
    if position <= -cap:
        return BUY
    return previous_direction


def initial_taker_direction(position, ordinal):
    if position > 0:
        return SELL
    if position < 0:
        return BUY
    return BUY if ordinal % 2 == 0 else SELL


def apply_local_fill(position, side, quantity):
    return position + quantity if side == BUY else position - quantity


def record_position_update(pos, prev, fills, idx, cur):
    pos[idx] = cur
    fills[0] += abs(cur - prev[idx])
    prev[idx] = cur


def tx_hex(raw_tx):
    value = raw_tx.hex()
    return value if value.startswith("0x") else f"0x{value}"


def error_key(exc):
    msg = str(exc)
    lower = msg.lower()
    if "insufficient margin" in lower:
        return "insufficient_margin"
    if "nonce" in lower:
        return "nonce"
    if "too many open files" in lower:
        return "too_many_open_files"
    if "timeout" in lower:
        return "timeout"
    if "connection" in lower:
        return "connection"
    if "already known" in lower:
        return "already_known"
    return exc.__class__.__name__


def maker_size_after_error(key, current_size, min_size, backoff):
    if key != "insufficient_margin" or backoff >= 1:
        return current_size
    if backoff <= 0:
        raise ValueError("maker size backoff must be positive")
    if min_size <= 0:
        raise ValueError("maker min size must be positive")
    return max(float(min_size), float(current_size) * float(backoff))


def maker_cooldown_until(key, consecutive_errors, threshold, cooldown_sec, now):
    if key != "insufficient_margin" or threshold <= 0 or cooldown_sec <= 0:
        return 0.0
    if consecutive_errors < threshold:
        return 0.0
    return float(now) + float(cooldown_sec)


def record_error(stats, samples, prefix, exc):
    key = f"{prefix}:{error_key(exc)}"
    stats[key] += 1
    samples.setdefault(key, str(exc)[:240])


def submit_success_counter(error_prefix):
    role = error_prefix.removesuffix("_error")
    return f"{role}_submit_ok"


def summary_counts(stats):
    keys = (
        "submit_ok",
        "taker_submit_ok",
        "maker_submit_ok",
        "taker_sent",
        "cancel_ok",
        "maker_size_backoff",
        "maker_cooldown",
        "maker_cooldown_skipped",
        "prep_skipped",
        "health_maker_skipped",
    )
    return {key: stats.get(key, 0) for key in keys}


def latency_bucket(elapsed):
    elapsed_ms = elapsed * 1000
    for threshold in (10, 50, 100, 250, 500, 1000, 2000):
        if elapsed_ms < threshold:
            return f"lt_{threshold}ms"
    return "ge_2000ms"


def record_latency(stats, role, elapsed):
    bucket = latency_bucket(elapsed)
    stats[f"{role}_latency:{bucket}"] += 1
    stats[f"{role}_latency_count"] += 1
    stats[f"{role}_latency_total_ms"] += elapsed * 1000


def latency_summary(stats, role):
    count = int(stats.get(f"{role}_latency_count", 0))
    if count == 0:
        return {"count": 0, "avg_ms": 0.0, "buckets": {}}
    prefix = f"{role}_latency:"
    buckets = {
        key.removeprefix(prefix): int(value)
        for key, value in stats.items()
        if key.startswith(prefix)
    }
    return {
        "count": count,
        "avg_ms": round(stats.get(f"{role}_latency_total_ms", 0.0) / count, 1),
        "buckets": buckets,
    }


def market_depth(lvl2_data, market_id):
    for market in lvl2_data or []:
        ident = market.get("marketId", market.get("symbol"))
        if str(ident) != str(market_id):
            continue
        bid_qty = sum(float(level[1]) for level in market.get("bids") or [])
        ask_qty = sum(float(level[1]) for level in market.get("asks") or [])
        return bid_qty, ask_qty
    return 0.0, 0.0


def has_book_liquidity(lvl2_data, market_id, min_bid_qty, min_ask_qty):
    bid_qty, ask_qty = market_depth(lvl2_data, market_id)
    return bid_qty >= min_bid_qty and ask_qty >= min_ask_qty


def next_time_nonce_value(previous, now_ms, state_nonce=0):
    return max(int(now_ms), int(previous or 0) + 1, int(state_nonce or 0) + 1)


def allocate_time_nonce(account):
    previous = getattr(account, "_match_time_nonce", 0)
    state_nonce = getattr(account, "_nonce", 0) or 0
    now_ms = int(time.time() * 1000)
    nonce = next_time_nonce_value(previous, now_ms, state_nonce)
    account._match_time_nonce = nonce
    return nonce


def sign_dex_tx(account, cmd_byte, payload, nonce_mode):
    if nonce_mode == "time":
        return account.sign_dex_tx(cmd_byte, payload, nonce=allocate_time_nonce(account))
    return account.sign_dex_tx(cmd_byte, payload)


def make_requests_session(pool_size):
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        pool_block=True,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class AsyncRpc:
    def __init__(
        self,
        rpc_url,
        concurrency=256,
        timeout=10,
        connections=None,
        resolve_once=False,
    ):
        self.rpc_url = rpc_url
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.connections = connections if connections is not None else min(concurrency, 128)
        self.resolver = StaticResolver.from_url(rpc_url) if resolve_once else None
        self.session = None
        self._next_id = 1

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(
            limit=self.connections,
            limit_per_host=self.connections,
            resolver=self.resolver,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=self.timeout,
            headers={"content-type": "application/json"},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    async def call(self, method, params):
        async with self.sem:
            req_id = self._next_id
            self._next_id += 1
            payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            async with self.session.post(self.rpc_url, json=payload) as resp:
                data = await resp.json(content_type=None)
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']}")
        return data.get("result")

    async def send_raw_transaction(self, raw_tx):
        return await self.call("eth_sendRawTransaction", [tx_hex(raw_tx)])


class StaticResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, host_map):
        self.host_map = host_map

    @classmethod
    def from_url(cls, url):
        host = urlsplit(url).hostname
        if not host:
            return None
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ip = infos[0][4][0]
        return cls({host: ip})

    async def resolve(self, host, port=0, family=socket.AF_INET):
        ip = self.host_map.get(host)
        if not ip:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            ip = infos[0][4][0]
        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": family,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self):
        return None


def order_payload(address, market_id, side, price, quantity, tif):
    return {
        "l1owner": address,
        "marketId": int(market_id),
        "side": int(side),
        "price": dec_str(price),
        "quantity": dec_str(quantity),
        "isReduceOnly": False,
        "timeInForce": int(tif),
    }


def cancel_all_payload(address, market_id):
    return {"l1owner": address, "marketId": int(market_id)}


async def submit_order(rpc, account, market_id, side, price, quantity, tif, stats, role="maker", nonce_mode="normal"):
    sign_started = time.perf_counter()
    raw = sign_dex_tx(
        account,
        CMD_PERP_ORDER,
        order_payload(account.address, market_id, side, price, quantity, tif),
        nonce_mode,
    )
    record_latency(stats, f"{role}_sign", time.perf_counter() - sign_started)
    started = time.perf_counter()
    try:
        await rpc.send_raw_transaction(raw)
    finally:
        record_latency(stats, role, time.perf_counter() - started)
    stats["submit_ok"] += 1
    stats[f"{role}_submit_ok"] += 1


async def send_raw_counted(rpc, raw, stats, samples, prefix, account=None, nonce_mode="normal"):
    role = prefix.removesuffix("_error")
    started = time.perf_counter()
    try:
        await rpc.send_raw_transaction(raw)
        record_latency(stats, role, time.perf_counter() - started)
        stats["submit_ok"] += 1
        stats[submit_success_counter(prefix)] += 1
    except Exception as exc:
        record_latency(stats, role, time.perf_counter() - started)
        record_error(stats, samples, prefix, exc)
        if account is not None and nonce_mode != "time" and error_key(exc) == "nonce":
            await resync_nonce_async(account, stats)


async def submit_cancel_all(rpc, account, market_id, stats, nonce_mode="normal"):
    sign_started = time.perf_counter()
    raw = sign_dex_tx(account, CMD_PERP_CANCEL_ALL, cancel_all_payload(account.address, market_id), nonce_mode)
    record_latency(stats, "cancel_sign", time.perf_counter() - sign_started)
    started = time.perf_counter()
    try:
        await rpc.send_raw_transaction(raw)
    finally:
        record_latency(stats, "cancel", time.perf_counter() - started)
    stats["cancel_ok"] += 1


async def post_maker_liquidity(rpc, account, market_id, ask_prices, bid_prices, size, stats, nonce_mode="normal"):
    for price in ask_prices:
        await submit_order(rpc, account, market_id, SELL, price, size, POST, stats, nonce_mode=nonce_mode)
    for price in bid_prices:
        await submit_order(rpc, account, market_id, BUY, price, size, POST, stats, nonce_mode=nonce_mode)


async def resync_nonce_async(account, stats):
    await asyncio.to_thread(account.resync_nonce)
    stats["nonce_resync"] += 1


def position_size_from_positions(positions, market_id):
    for item in positions:
        if int(item["marketId"]) == market_id:
            return _to_int(item["size"]) / 1e18
    return 0.0


def account_health(account, market_id):
    perp_account = account.perp_account() or {}
    wallet = _to_int(perp_account.get("walletBalance")) / 1e18
    order_margin = _to_int(perp_account.get("orderMargin")) / 1e18
    position = position_size_from_positions(account.positions() or [], market_id)
    return {
        "wallet": wallet,
        "order_margin": order_margin,
        "free": wallet - order_margin,
        "position": position,
    }


async def account_position(rpc, address, market_id):
    positions = await rpc.call("arb_getPerpPositions", [address])
    return position_size_from_positions(positions or [], market_id)


async def gather_limited(items, limit, func):
    sem = asyncio.Semaphore(limit)

    async def run(item):
        async with sem:
            return await func(item)

    return await asyncio.gather(*(run(item) for item in items), return_exceptions=True)


async def poll_positions_once(rpc, accounts, market_id, pos, prev, fills, taker_offset, stats, samples):
    for idx, account in position_poll_items(accounts, taker_offset):
        try:
            cur = await account_position(rpc, account.address, market_id)
            record_position_update(pos, prev, fills, idx, cur)
        except Exception as exc:
            record_error(stats, samples, "poll_error", exc)


async def poll_positions(rpc, accounts, market_id, pos, prev, fills, taker_offset, stop, stats, samples, interval):
    while not stop.is_set():
        await poll_positions_once(rpc, accounts, market_id, pos, prev, fills, taker_offset, stats, samples)
        await asyncio.sleep(interval)


async def poll_book_guard(rpc, market_id, min_bid_qty, min_ask_qty, state, stop, stats, samples, interval):
    while not stop.is_set():
        try:
            data = await rpc.call("arb_getPerpLvl2Data", [])
            bid_qty, ask_qty = market_depth(data, market_id)
            state["bid_qty"] = bid_qty
            state["ask_qty"] = ask_qty
            state["refresh_needed"] = bid_qty < min_bid_qty or ask_qty < min_ask_qty
            stats["book_guard_ok"] += 1
        except Exception as exc:
            state["refresh_needed"] = True
            record_error(stats, samples, "book_guard_error", exc)
        await asyncio.sleep(interval)


async def maker_loop(
    rpc,
    account,
    market_id,
    ask_prices,
    bid_prices,
    size,
    interval,
    cancel_every,
    initial_delay,
    guard_state,
    nonce_mode,
    stop,
    stats,
    samples,
    min_size=None,
    size_backoff=1.0,
    error_cooldown_threshold=0,
    error_cooldown_sec=0.0,
):
    next_at = time.perf_counter()
    if initial_delay > 0:
        next_at += initial_delay
        await asyncio.sleep(initial_delay)
    tick = 0
    current_size = size
    min_size = size if min_size is None else min_size
    consecutive_margin_errors = 0
    cooldown_until = 0.0
    while not stop.is_set():
        try:
            now = time.perf_counter()
            if cooldown_until > now:
                stats["maker_cooldown_skipped"] += 1
                next_at = max(next_at + interval, cooldown_until)
                await asyncio.sleep(max(0.0, next_at - time.perf_counter()))
                continue
            if guard_state is not None and not guard_state.get("refresh_needed", True):
                stats["maker_refresh_skipped"] += 1
            else:
                if cancel_every > 0 and tick % cancel_every == 0:
                    await submit_cancel_all(rpc, account, market_id, stats, nonce_mode)
                await post_maker_liquidity(rpc, account, market_id, ask_prices, bid_prices, current_size, stats, nonce_mode)
                consecutive_margin_errors = 0
        except Exception as exc:
            key = error_key(exc)
            record_error(stats, samples, "maker_error", exc)
            if key == "insufficient_margin":
                consecutive_margin_errors += 1
            else:
                consecutive_margin_errors = 0
            adjusted_size = maker_size_after_error(key, current_size, min_size, size_backoff)
            if adjusted_size < current_size:
                current_size = adjusted_size
                stats["maker_size_backoff"] += 1
            next_cooldown_until = maker_cooldown_until(
                key,
                consecutive_margin_errors,
                error_cooldown_threshold,
                error_cooldown_sec,
                time.perf_counter(),
            )
            if next_cooldown_until:
                cooldown_until = next_cooldown_until
                stats["maker_cooldown"] += 1
                consecutive_margin_errors = 0
            if nonce_mode != "time":
                await resync_nonce_async(account, stats)
        tick += 1
        next_at += interval
        await asyncio.sleep(max(0.0, next_at - time.perf_counter()))


async def taker_loop(
    rpc,
    account,
    market_id,
    local_pos,
    pos_index,
    cap,
    buy_price,
    sell_price,
    size,
    interval,
    max_inflight,
    initial_direction,
    nonce_mode,
    stop,
    stats,
    samples,
):
    next_at = time.perf_counter()
    direction = initial_direction
    inflight = set()
    try:
        while not stop.is_set():
            if len(inflight) >= max_inflight:
                wait_started = time.perf_counter()
                done, inflight = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
                record_latency(stats, "taker_inflight_wait", time.perf_counter() - wait_started)
                for task in done:
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        record_error(stats, samples, "taker_task_error", exc)
                inflight = {task for task in inflight if not task.done()}
            direction = choose_direction(local_pos[pos_index], cap, direction)
            price = buy_price if direction == BUY else sell_price
            try:
                sign_started = time.perf_counter()
                raw = sign_dex_tx(
                    account,
                    CMD_PERP_ORDER,
                    order_payload(account.address, market_id, direction, price, size, IOC),
                    nonce_mode,
                )
                record_latency(stats, "taker_sign", time.perf_counter() - sign_started)
                task = asyncio.create_task(send_raw_counted(rpc, raw, stats, samples, "taker_error", account, nonce_mode))
                inflight.add(task)
                local_pos[pos_index] = apply_local_fill(local_pos[pos_index], direction, size)
                stats["taker_sent"] += 1
            except Exception as exc:
                record_error(stats, samples, "taker_error", exc)
                if nonce_mode != "time":
                    await resync_nonce_async(account, stats)
            next_at += interval
            await asyncio.sleep(max(0.0, next_at - time.perf_counter()))
    finally:
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)


async def reporter(pos, taker_offset, fills, taker_size, cap, stats, stop, started_at):
    prev_stats = Counter()
    prev_elapsed = 0.0
    while not stop.is_set():
        await asyncio.sleep(5)
        elapsed = time.time() - started_at
        delta_t = max(0.001, elapsed - prev_elapsed)
        snapshot = Counter(stats)
        delta = snapshot - prev_stats
        inv = pos[taker_offset:] or [0.0]
        trades = fills[0] / taker_size if taker_size else 0.0
        errors = sum(v for k, v in delta.items() if "error" in k)
        taker_latency = latency_summary(delta, "taker")
        maker_latency = latency_summary(delta, "maker")
        taker_sign_latency = latency_summary(delta, "taker_sign")
        maker_sign_latency = latency_summary(delta, "maker_sign")
        taker_wait_latency = latency_summary(delta, "taker_inflight_wait")
        print(
            f"t={elapsed:.0f}s trades={trades:.0f} rate={trades / max(elapsed, 0.001):.1f}/s "
            f"submit/s={delta.get('submit_ok', 0) / delta_t:.1f} "
            f"taker/s={delta.get('taker_submit_ok', 0) / delta_t:.1f} "
            f"maker/s={delta.get('maker_submit_ok', 0) / delta_t:.1f} "
            f"maker_skip/s={delta.get('maker_refresh_skipped', 0) / delta_t:.1f} "
            f"taker_ms={taker_latency['avg_ms']:.0f} maker_ms={maker_latency['avg_ms']:.0f} "
            f"sign_ms={taker_sign_latency['avg_ms']:.1f}/{maker_sign_latency['avg_ms']:.1f} "
            f"wait_ms={taker_wait_latency['avg_ms']:.0f} "
            f"err/s={errors / delta_t:.1f} "
            f"taker_inv=[{min(inv):+.4f},{max(inv):+.4f}] cap=+/-{cap}"
        )
        prev_stats = snapshot
        prev_elapsed = elapsed


async def amain():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_role_config(path, "maker")
    target = float(sys.argv[2]) if len(sys.argv) > 2 else float(cfg.get("match_tps", 20))
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("match_duration", 0))

    per_acct = env_float("MATCH_PER_ACCOUNT_TPS", cfg.get("per_account_tps", 4))
    lev = int(cfg["leverage"])
    deposit = env_float("MATCH_DEPOSIT", cfg.get("match_deposit", 100000))
    maker_deposit, taker_deposit = role_deposit_targets(
        deposit,
        maker_override=os.environ.get("MATCH_MAKER_DEPOSIT"),
        taker_override=os.environ.get("MATCH_TAKER_DEPOSIT"),
    )
    gas_eth = env_float("MATCH_GAS_ETH", cfg.get("match_gas_eth", 0.1))
    base_size = float(cfg.get("order_size", 0.01))
    maker_size = env_float("MATCH_MAKER_SIZE", cfg.get("maker_order_size", base_size))
    taker_size = env_float("MATCH_TAKER_SIZE", cfg.get("taker_order_size", base_size))
    maker_min_size = env_float("MATCH_MAKER_MIN_SIZE", maker_size)
    maker_size_backoff = env_float("MATCH_MAKER_SIZE_BACKOFF", 1.0)
    maker_error_cooldown_threshold = env_int(
        "MATCH_MAKER_ERROR_COOLDOWN_THRESHOLD",
        cfg.get("maker_error_cooldown_threshold", 0),
    )
    maker_error_cooldown_sec = env_float(
        "MATCH_MAKER_ERROR_COOLDOWN_SEC",
        cfg.get("maker_error_cooldown_sec", 0.0),
    )
    skip_prep_failed = env_int("MATCH_SKIP_PREP_FAILED", cfg.get("skip_prep_failed", 0)) != 0
    cap = env_float("MATCH_INVENTORY_CAP", cfg.get("inventory_cap", 0.5))
    spread = float(cfg.get("spread", 0.0005))
    slip = float(cfg.get("taker_slippage", 0.01))
    market_id = int(cfg["market_id"])
    maker_count, taker_count = role_account_counts(
        target,
        per_acct,
        total_override=os.environ.get("MATCH_TOTAL_PAIRS"),
        maker_override=os.environ.get("MATCH_MAKER_COUNT"),
        taker_override=os.environ.get("MATCH_TAKER_COUNT"),
    )
    maker_pool_count = env_int("MATCH_MAKER_POOL_COUNT", cfg.get("maker_pool_count", maker_count))
    healthy_maker_min_free = env_float(
        "MATCH_HEALTHY_MAKER_MIN_FREE",
        cfg.get("healthy_maker_min_free", 0.0),
    )
    healthy_maker_max_abs_pos = env_float(
        "MATCH_HEALTHY_MAKER_MAX_ABS_POS",
        cfg.get("healthy_maker_max_abs_pos", 1e9),
    )
    maker_pool_count = max(maker_count, maker_pool_count)

    worker_index = env_int("MATCH_WORKER_INDEX", 0)
    worker_count = env_int("MATCH_WORKER_COUNT", 1)
    maker_start, maker_end = worker_bounds(maker_count, worker_index, worker_count)
    taker_start, taker_end = worker_bounds(taker_count, worker_index, worker_count)
    local_maker_count = maker_end - maker_start
    local_taker_count = taker_end - taker_start
    if local_maker_count <= 0 and local_taker_count <= 0:
        raise SystemExit(f"worker {worker_index}/{worker_count} has no accounts")

    rpc_concurrency = env_int("MATCH_RPC_CONCURRENCY", 512)
    rpc_connections = env_int("MATCH_RPC_CONNECTIONS", min(rpc_concurrency, 128))
    resolve_once = env_int("MATCH_RPC_RESOLVE_ONCE", cfg.get("rpc_resolve_once", 0)) != 0
    setup_concurrency = env_int("MATCH_SETUP_CONCURRENCY", 8)
    rpc_timeout = env_float("MATCH_RPC_TIMEOUT", 10)
    poll_interval = env_float("MATCH_POLL_INTERVAL", 1.0)
    position_poll_mode = normalize_position_poll_mode(
        os.environ.get("MATCH_POSITION_POLL_MODE", cfg.get("position_poll_mode", "continuous"))
    )
    maker_cancel_every = env_int("MATCH_MAKER_CANCEL_EVERY", cfg.get("maker_cancel_every", 20))
    maker_mode = os.environ.get("MATCH_MAKER_MODE", cfg.get("maker_mode", "refresh")).lower()
    taker_mode = os.environ.get("MATCH_TAKER_MODE", cfg.get("taker_mode", "on")).lower()
    maker_refresh_sec = env_float("MATCH_MAKER_REFRESH_SEC", cfg.get("maker_refresh_sec", 2.0))
    maker_stagger = mode_enabled(os.environ.get("MATCH_MAKER_STAGGER", cfg.get("maker_stagger", "0")))
    account_inflight = env_int("MATCH_ACCOUNT_INFLIGHT", cfg.get("account_inflight", 1))
    maker_guard = env_int("MATCH_MAKER_GUARD", cfg.get("maker_guard", 0)) != 0
    maker_guard_poll_sec = env_float("MATCH_MAKER_GUARD_POLL_SEC", cfg.get("maker_guard_poll_sec", 0.5))
    guard_default_qty = maker_size * max(1, local_maker_count) * 0.25
    maker_guard_min_bid = env_float("MATCH_MAKER_GUARD_MIN_BID", cfg.get("maker_guard_min_bid", guard_default_qty))
    maker_guard_min_ask = env_float("MATCH_MAKER_GUARD_MIN_ASK", cfg.get("maker_guard_min_ask", guard_default_qty))
    nonce_mode = os.environ.get("MATCH_NONCE_MODE", cfg.get("nonce_mode", "normal")).lower()
    if nonce_mode not in ("normal", "time"):
        raise ValueError("MATCH_NONCE_MODE must be normal or time")

    web3_session = make_requests_session(setup_concurrency)
    web3_request_kwargs = {"timeout": rpc_timeout}
    dev = PerpDexClient(
        cfg["rpc_url"],
        cfg["private_key"],
        cfg["dex_address"],
        session=web3_session,
        request_kwargs=web3_request_kwargs,
    )
    market_info = dev.market_info(market_id)
    tick = market_info["tick"] if market_info else float(cfg.get("tick_size", 1))
    band_bps = (market_info["price_band_bps"] if market_info else 0) or DEFAULT_BAND_BPS
    mark = dev.mark_price(market_id) or float(cfg["ref_price"])
    low, high = band_bounds(mark, band_bps)
    quantize = lambda price: round(price / tick) * tick

    levels = env_int("MATCH_LEVELS", cfg.get("match_levels", 1))
    step = float(cfg.get("level_step", 0.001))
    ask_prices = [quantize(mark * (1 + spread + i * step)) for i in range(levels)]
    bid_prices = [quantize(mark * (1 - spread - i * step)) for i in range(levels)]
    taker_buy = min(math.floor(high / tick) * tick - tick, quantize(mark * (1 + slip)))
    taker_sell = max(math.ceil(low / tick) * tick + tick, quantize(mark * (1 - slip)))

    keystore = cfg.get("keystore", "accounts.json")
    all_makers = load_or_create(keystore, "maker", maker_pool_count)
    all_takers = load_or_create(keystore, "taker", taker_count)
    worker_takers = all_takers[taker_start:taker_end]
    health_filter_enabled = (
        maker_pool_count > maker_count
        or healthy_maker_min_free > 0
        or healthy_maker_max_abs_pos < 1e9
    )
    health_maker_skipped = 0
    if health_filter_enabled:
        maker_candidates = [
            PerpDexClient(
                cfg["rpc_url"],
                item["key"],
                cfg["dex_address"],
                session=web3_session,
                request_kwargs=web3_request_kwargs,
            )
            for item in all_makers
        ]
        maker_health = await gather_limited(
            maker_candidates,
            setup_concurrency,
            lambda account: asyncio.to_thread(account_health, account, market_id),
        )
        selected_makers, health_maker_skipped = select_healthy_accounts(
            maker_candidates,
            maker_health,
            maker_count,
            healthy_maker_min_free,
            healthy_maker_max_abs_pos,
        )
        if len(selected_makers) < maker_count:
            raise RuntimeError(
                f"only {len(selected_makers)} healthy makers available, need {maker_count}"
            )
        makers = selected_makers[maker_start:maker_end]
        print(
            f"healthy maker pool selected {len(selected_makers)}/{maker_pool_count} "
            f"(skipped={health_maker_skipped}, min_free={healthy_maker_min_free}, "
            f"max_abs_pos={healthy_maker_max_abs_pos})"
        )
    else:
        worker_makers = all_makers[maker_start:maker_end]
        makers = [
            PerpDexClient(
                cfg["rpc_url"],
                item["key"],
                cfg["dex_address"],
                session=web3_session,
                request_kwargs=web3_request_kwargs,
            )
            for item in worker_makers
        ]
    takers = [
        PerpDexClient(
            cfg["rpc_url"],
            item["key"],
            cfg["dex_address"],
            session=web3_session,
            request_kwargs=web3_request_kwargs,
        )
        for item in worker_takers
    ]
    active_roles = active_role_names(maker_mode, taker_mode)
    active_makers = makers if "maker" in active_roles else []
    active_takers = takers if "taker" in active_roles else []
    accounts = active_makers + active_takers

    print(
        f"match_async: target={target} trades/s makers={maker_count} takers={taker_count} worker={worker_index}/{worker_count} "
        f"maker_range={maker_start}:{maker_end} taker_range={taker_start}:{taker_end} "
        f"local_makers={local_maker_count} local_takers={local_taker_count} per_account_tps={per_acct} market={market_id} "
        f"mark={mark} levels={levels} maker_size={maker_size} maker_min_size={maker_min_size} "
        f"maker_size_backoff={maker_size_backoff} taker_size={taker_size} "
        f"maker_mode={maker_mode} taker_mode={taker_mode} active_roles={active_roles} "
        f"maker_refresh_sec={maker_refresh_sec} maker_stagger={maker_stagger} "
        f"account_inflight={account_inflight} maker_guard={maker_guard} poll_mode={position_poll_mode} "
        f"maker_error_cooldown={maker_error_cooldown_threshold}/{maker_error_cooldown_sec}s "
        f"skip_prep_failed={skip_prep_failed} "
        f"maker_pool={maker_pool_count} "
        f"healthy_maker_min_free={healthy_maker_min_free} "
        f"healthy_maker_max_abs_pos={healthy_maker_max_abs_pos} "
        f"maker_deposit={maker_deposit} taker_deposit={taker_deposit} "
        f"nonce_mode={nonce_mode} "
        f"guard_min_bid={maker_guard_min_bid} guard_min_ask={maker_guard_min_ask} "
        f"rpc={cfg['rpc_url']} concurrency={rpc_concurrency}"
    )

    if active_makers:
        ensure_funded(dev, active_makers, gas_eth=gas_eth, deposit=maker_deposit)
    if active_takers:
        ensure_funded(dev, active_takers, gas_eth=gas_eth, deposit=taker_deposit)

    def prep(account):
        ok = True
        for fn in (
            lambda: account.cancel_all(market_id),
            lambda: account.set_leverage(market_id, lev),
        ):
            try:
                fn()
            except Exception as exc:
                ok = False
                print("prep warn", account.address[:10], exc)
        account.prime()
        return ok

    maker_prep_results = await gather_limited(
        active_makers,
        setup_concurrency,
        lambda account: asyncio.to_thread(prep, account),
    )
    taker_prep_results = await gather_limited(
        active_takers,
        setup_concurrency,
        lambda account: asyncio.to_thread(prep, account),
    )
    active_makers, skipped_makers = filter_prepped_accounts(active_makers, maker_prep_results, skip_prep_failed)
    active_takers, skipped_takers = filter_prepped_accounts(active_takers, taker_prep_results, skip_prep_failed)
    if skip_prep_failed and (skipped_makers or skipped_takers):
        print(f"prep filtered accounts: makers={skipped_makers} takers={skipped_takers}")
    if "maker" in active_roles and not active_makers:
        raise RuntimeError("no maker accounts remain after prep filtering")
    if "taker" in active_roles and not active_takers:
        raise RuntimeError("no taker accounts remain after prep filtering")
    accounts = active_makers + active_takers
    local_maker_count = len(active_makers)
    local_taker_count = len(active_takers)
    if nonce_mode == "time":
        seed = int(time.time() * 1000)
        for account in accounts:
            account._match_time_nonce = max(seed, getattr(account, "_nonce", 0) or 0)
    print(
        f"ready {len(accounts)} accounts "
        f"(maker deposit target={maker_deposit}, taker deposit target={taker_deposit})"
    )

    pos = [0.0] * len(accounts)
    prev = [0.0] * len(accounts)
    fills = [0.0]
    stop = asyncio.Event()
    stats = Counter()
    stats["prep_skipped"] = skipped_makers + skipped_takers
    stats["health_maker_skipped"] = health_maker_skipped
    samples = {}
    local_target = target / worker_count
    interval = local_taker_count / local_target if local_target > 0 and local_taker_count > 0 else 1
    taker_offset = len(active_makers)

    async with AsyncRpc(
        cfg["rpc_url"],
        concurrency=rpc_concurrency,
        timeout=rpc_timeout,
        connections=rpc_connections,
        resolve_once=resolve_once,
    ) as rpc:
        initial_position_items = position_poll_items(accounts, taker_offset)
        initial_positions = await gather_limited(
            initial_position_items,
            setup_concurrency,
            lambda item: account_position(rpc, item[1].address, market_id),
        )
        for (idx, account), value in zip(initial_position_items, initial_positions):
            if isinstance(value, Exception):
                print("initial position warn", account.address[:10], value)
                continue
            pos[idx] = value
            prev[idx] = value

            tasks = []
            if position_poll_mode == "continuous":
                tasks.append(
                    asyncio.create_task(
                        poll_positions(
                            rpc,
                            accounts,
                            market_id,
                            pos,
                            prev,
                            fills,
                            taker_offset,
                            stop,
                            stats,
                            samples,
                            poll_interval,
                        )
                    )
                )
        maker_guard_state = None
        if maker_guard:
            maker_guard_state = {"refresh_needed": True, "bid_qty": 0.0, "ask_qty": 0.0}
            tasks.append(
                asyncio.create_task(
                    poll_book_guard(
                        rpc,
                        market_id,
                        maker_guard_min_bid,
                        maker_guard_min_ask,
                        maker_guard_state,
                        stop,
                        stats,
                        samples,
                        maker_guard_poll_sec,
                    )
                )
            )
        if maker_mode not in ("refresh", "once", "off"):
            raise ValueError("MATCH_MAKER_MODE must be refresh, once, or off")
        if maker_mode == "once":
            async def seed_maker(account):
                try:
                    await submit_cancel_all(rpc, account, market_id, stats, nonce_mode)
                    await post_maker_liquidity(rpc, account, market_id, ask_prices, bid_prices, maker_size, stats, nonce_mode)
                except Exception as exc:
                    record_error(stats, samples, "maker_seed_error", exc)
                    if nonce_mode != "time":
                        await resync_nonce_async(account, stats)

            await asyncio.gather(*(seed_maker(account) for account in active_makers))
            print(f"seeded maker liquidity once for {len(active_makers)} makers")
        elif maker_mode == "refresh":
            for idx, account in enumerate(active_makers):
                tasks.append(
                    asyncio.create_task(
                        maker_loop(
                            rpc,
                            account,
                            market_id,
                            ask_prices,
                            bid_prices,
                            maker_size,
                            maker_refresh_sec,
                            maker_cancel_every,
                            stagger_delay(idx, len(active_makers), maker_refresh_sec) if maker_stagger else 0.0,
                            maker_guard_state,
                            nonce_mode,
                            stop,
                            stats,
                        samples,
                        maker_min_size,
                        maker_size_backoff,
                        maker_error_cooldown_threshold,
                        maker_error_cooldown_sec,
                    )
                )
            )
        started_at = time.time()
        tasks.append(asyncio.create_task(reporter(pos, taker_offset, fills, taker_size, cap, stats, stop, started_at)))

        if mode_enabled(taker_mode):
            for idx, account in enumerate(active_takers):
                pos_index = taker_offset + idx
                tasks.append(
                    asyncio.create_task(
                        taker_loop(
                            rpc,
                            account,
                            market_id,
                            pos,
                            pos_index,
                            cap,
                            taker_buy,
                            taker_sell,
                            taker_size,
                            interval,
                            account_inflight,
                            initial_taker_direction(pos[pos_index], idx),
                        nonce_mode,
                        stop,
                        stats,
                        samples,
                    )
                )
            )

        try:
            if duration > 0:
                await asyncio.sleep(duration)
            else:
                while True:
                    await asyncio.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            await asyncio.sleep(1)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            elapsed = time.time() - started_at
            if position_poll_mode in ("continuous", "final"):
                await poll_positions_once(rpc, accounts, market_id, pos, prev, fills, taker_offset, stats, samples)

            inv = pos[taker_offset:] or [0.0]
            trades = fills[0] / taker_size if taker_size else 0.0
        error_summary = {k: v for k, v in stats.items() if "error" in k}
        counts = summary_counts(stats)
        latency = {
            "taker": latency_summary(stats, "taker"),
            "maker": latency_summary(stats, "maker"),
            "cancel": latency_summary(stats, "cancel"),
            "taker_sign": latency_summary(stats, "taker_sign"),
            "maker_sign": latency_summary(stats, "maker_sign"),
            "cancel_sign": latency_summary(stats, "cancel_sign"),
            "taker_inflight_wait": latency_summary(stats, "taker_inflight_wait"),
        }
        latency_avg_ms = {key: value["avg_ms"] for key, value in latency.items()}
        print(
            f"DONE: {trades:.0f} fills in {elapsed:.0f}s = {trades / max(elapsed, 0.001):.1f} trades/s; "
            f"submit_ok={counts['submit_ok']} taker_submit_ok={counts['taker_submit_ok']} "
            f"maker_submit_ok={counts['maker_submit_ok']} taker_sent={counts['taker_sent']} "
            f"cancel_ok={counts['cancel_ok']} maker_refresh_skipped={stats.get('maker_refresh_skipped', 0)} "
            f"maker_size_backoff={counts['maker_size_backoff']} "
            f"maker_cooldown={counts['maker_cooldown']} "
            f"maker_cooldown_skipped={counts['maker_cooldown_skipped']} "
            f"prep_skipped={counts['prep_skipped']} "
            f"health_maker_skipped={counts['health_maker_skipped']} "
            f"book_guard_ok={stats.get('book_guard_ok', 0)} "
            f"errors={error_summary} latency_avg_ms={latency_avg_ms} latency={latency} samples={samples} "
            f"final taker_inv range [{min(inv):+.4f},{max(inv):+.4f}]"
        )


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()

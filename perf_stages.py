#!/usr/bin/env python3
"""Run and summarize staged perf load tests against the configured RPC path."""

import argparse
import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DONE_RE = re.compile(
    r"DONE:\s+(?P<fills>\d+)\s+fills\s+in\s+(?P<elapsed>\d+)s\s+=\s+"
    r"(?P<trades_s>[0-9.]+)\s+trades/s;\s+(?P<rest>.*)"
)
COUNTER_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>-?\d+)")


BASE_ENV = {
    "PYTHONUNBUFFERED": "1",
    "MATCH_MAKER_MODE": "refresh",
    "MATCH_MAKER_SIZE": "0.005",
    "MATCH_TAKER_SIZE": "0.001",
    "MATCH_INVENTORY_CAP": "0.25",
    "MATCH_PER_ACCOUNT_TPS": "8",
    "MATCH_RPC_CONCURRENCY": "512",
    "MATCH_RPC_CONNECTIONS": "64",
    "MATCH_SETUP_CONCURRENCY": "2",
    "MATCH_RPC_RESOLVE_ONCE": "1",
    "MATCH_POLL_INTERVAL": "2",
    "MATCH_MAKER_REFRESH_SEC": "2",
    "MATCH_MAKER_CANCEL_EVERY": "20",
}


STAGES = {
    "baseline": {
        "description": "safe baseline, normal nonce, one in-flight per account",
        "env": {
            "MATCH_NONCE_MODE": "normal",
            "MATCH_ACCOUNT_INFLIGHT": "1",
            "MATCH_MAKER_GUARD": "0",
        },
    },
    "wide_accounts": {
        "description": "wide taker fanout, normal nonce, one in-flight per account",
        "env": {
            "MATCH_NONCE_MODE": "normal",
            "MATCH_ACCOUNT_INFLIGHT": "1",
            "MATCH_MAKER_GUARD": "0",
            "MATCH_TAKER_COUNT": "300",
            "MATCH_MAKER_COUNT": "40",
            "MATCH_PER_ACCOUNT_TPS": "1",
        },
    },
    "time_inflight2": {
        "description": "time nonce with two in-flight taker transactions per account",
        "env": {
            "MATCH_NONCE_MODE": "time",
            "MATCH_ACCOUNT_INFLIGHT": "2",
            "MATCH_MAKER_GUARD": "0",
        },
    },
    "maker_guard": {
        "description": "safe baseline with maker repost guard enabled",
        "env": {
            "MATCH_NONCE_MODE": "normal",
            "MATCH_ACCOUNT_INFLIGHT": "1",
            "MATCH_MAKER_GUARD": "1",
        },
    },
    "maker_adaptive": {
        "description": "safe baseline with per-maker size backoff on insufficient margin",
        "env": {
            "MATCH_NONCE_MODE": "normal",
            "MATCH_ACCOUNT_INFLIGHT": "1",
            "MATCH_MAKER_GUARD": "0",
            "MATCH_MAKER_MIN_SIZE": "0.001",
            "MATCH_MAKER_SIZE_BACKOFF": "0.5",
        },
    },
    "final_poll": {
        "description": "safe baseline with final-only position polling to reduce read RPC during load",
        "env": {
            "MATCH_NONCE_MODE": "normal",
            "MATCH_ACCOUNT_INFLIGHT": "1",
            "MATCH_MAKER_GUARD": "0",
            "MATCH_POSITION_POLL_MODE": "final",
            "MATCH_INVENTORY_CAP": "1.0",
        },
    },
    "maker_cooldown": {
        "description": "final-only polling with cooldown after repeated maker margin errors",
        "env": {
            "MATCH_NONCE_MODE": "normal",
            "MATCH_ACCOUNT_INFLIGHT": "1",
            "MATCH_MAKER_GUARD": "0",
            "MATCH_POSITION_POLL_MODE": "final",
            "MATCH_INVENTORY_CAP": "1.0",
            "MATCH_MAKER_ERROR_COOLDOWN_THRESHOLD": "2",
            "MATCH_MAKER_ERROR_COOLDOWN_SEC": "10",
        },
    },
    "healthy_accounts": {
        "description": "final-only polling after excluding accounts that fail prep",
        "env": {
            "MATCH_NONCE_MODE": "normal",
            "MATCH_ACCOUNT_INFLIGHT": "1",
            "MATCH_MAKER_GUARD": "0",
            "MATCH_POSITION_POLL_MODE": "final",
            "MATCH_INVENTORY_CAP": "1.0",
            "MATCH_SKIP_PREP_FAILED": "1",
        },
    },
    "healthy_makers": {
        "description": "final-only polling with makers selected from a healthy account pool",
        "env": {
            "MATCH_NONCE_MODE": "normal",
            "MATCH_ACCOUNT_INFLIGHT": "1",
            "MATCH_MAKER_GUARD": "0",
            "MATCH_POSITION_POLL_MODE": "final",
            "MATCH_INVENTORY_CAP": "1.0",
            "MATCH_MAKER_POOL_COUNT": "150",
            "MATCH_HEALTHY_MAKER_MIN_FREE": "500",
            "MATCH_HEALTHY_MAKER_MAX_ABS_POS": "0.02",
        },
    },
}


def parse_done_line(line):
    match = DONE_RE.search(line)
    if not match:
        return None

    rest = match.group("rest")
    parsed = {
        "fills": int(match.group("fills")),
        "elapsed_s": int(match.group("elapsed")),
        "trades_s": float(match.group("trades_s")),
    }
    for counter in COUNTER_RE.finditer(rest):
        parsed[counter.group("key")] = int(counter.group("value"))

    errors_marker = "errors="
    if errors_marker in rest:
        start = rest.index(errors_marker) + len(errors_marker)
        end = rest.find(" latency_avg_ms=", start)
        if end == -1:
            end = rest.find(" latency=", start)
        if end == -1:
            end = rest.find(" samples=", start)
        if end == -1:
            end = len(rest)
        try:
            value = ast.literal_eval(rest[start:end].strip())
            if isinstance(value, dict):
                parsed["errors"] = value
        except (SyntaxError, ValueError):
            parsed["errors_parse_error"] = rest[start:end].strip()

    marker = "latency_avg_ms="
    if marker in rest:
        start = rest.index(marker) + len(marker)
        end = rest.find(" latency=", start)
        if end == -1:
            end = rest.find(" samples=", start)
        if end == -1:
            end = len(rest)
        try:
            value = ast.literal_eval(rest[start:end].strip())
            if isinstance(value, dict):
                parsed["latency_avg_ms"] = value
        except (SyntaxError, ValueError):
            parsed["latency_avg_ms_parse_error"] = rest[start:end].strip()

    return parsed


def parse_done_text(text):
    result = None
    for line in text.splitlines():
        parsed = parse_done_line(line)
        if parsed is not None:
            result = parsed
    return result


def stage_env(stage_name):
    env = dict(BASE_ENV)
    env.update(STAGES[stage_name]["env"])
    return env


def parse_env_overrides(items):
    overrides = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"env override must be KEY=VALUE: {item}")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"env override key cannot be empty: {item}")
        overrides[key] = value
    return overrides


def build_command(python_bin, config, target, duration):
    return [python_bin, "match.py", config, str(target), str(duration)]


def prepare_stage_config(config, fresh_keystore_dir, stem, write=True):
    if not fresh_keystore_dir:
        return config
    fresh_dir = Path(fresh_keystore_dir)
    config_path = fresh_dir / f"{stem}.config.json"
    keystore_path = fresh_dir / f"{stem}.accounts.json"
    if write:
        fresh_dir.mkdir(parents=True, exist_ok=True)
        with open(config, encoding="utf-8") as handle:
            data = json.load(handle)
        data["keystore"] = str(keystore_path)
        with config_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
    return str(config_path)


def parse_target_sweep(default_target, target_sweep):
    if not target_sweep:
        return [float(default_target)]
    targets = []
    for part in target_sweep.split(","):
        value = part.strip()
        if not value:
            continue
        target = float(value)
        if target <= 0:
            raise ValueError("target sweep values must be positive")
        targets.append(target)
    if not targets:
        raise ValueError("target sweep must include at least one value")
    return targets


def target_slug(target):
    return f"{float(target):g}".replace(".", "p")


def stage_log_stem(stage_name, timestamp, target, include_target):
    if include_target:
        return f"perf-stage-{stage_name}-t{target_slug(target)}-{timestamp}"
    return f"perf-stage-{stage_name}-{timestamp}"


def pprof_profile_url(url, seconds):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "seconds"]
    query.insert(0, ("seconds", str(int(seconds))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def build_pprof_command(url, seconds, output_path):
    return [
        "curl",
        "-fsSL",
        "--max-time",
        str(int(seconds) + 30),
        "-o",
        str(output_path),
        pprof_profile_url(url, seconds),
    ]


def stage_should_profile(args, stage_name):
    if not args.pprof_url:
        return False
    stages = {stage.strip() for stage in args.pprof_stages.split(",") if stage.strip()}
    return "all" in stages or stage_name in stages


AGGREGATE_COUNTERS = (
    "fills",
    "submit_ok",
    "taker_submit_ok",
    "maker_submit_ok",
    "taker_sent",
    "cancel_ok",
    "maker_refresh_skipped",
    "maker_size_backoff",
    "maker_cooldown",
    "maker_cooldown_skipped",
    "prep_skipped",
    "health_maker_skipped",
    "book_guard_ok",
)


def worker_env(overrides, worker_index, worker_count):
    env = dict(overrides)
    env["MATCH_WORKER_INDEX"] = str(worker_index)
    env["MATCH_WORKER_COUNT"] = str(worker_count)
    return env


def latency_weight(result, key):
    if key in ("taker", "taker_inflight_wait"):
        return result.get("taker_submit_ok") or result.get("taker_sent") or 0
    if key == "taker_sign":
        return result.get("taker_sent") or result.get("taker_submit_ok") or 0
    if key in ("maker", "maker_sign"):
        return result.get("maker_submit_ok") or 0
    if key in ("cancel", "cancel_sign"):
        return result.get("cancel_ok") or 0
    return 1


def aggregate_latency(worker_results):
    keys = set()
    for result in worker_results:
        keys.update(result.get("latency_avg_ms", {}).keys())

    latency = {}
    for key in sorted(keys):
        total = 0.0
        weight_total = 0
        fallback_values = []
        for result in worker_results:
            value = result.get("latency_avg_ms", {}).get(key)
            if value is None:
                continue
            weight = latency_weight(result, key)
            if weight:
                total += float(value) * weight
                weight_total += weight
            else:
                fallback_values.append(float(value))
        if weight_total:
            latency[key] = round(total / weight_total, 1)
        elif fallback_values:
            latency[key] = round(sum(fallback_values) / len(fallback_values), 1)
    return latency


def aggregate_worker_results(stage_name, target, worker_results):
    result = {
        "stage": stage_name,
        "target": float(target),
        "worker_count": len(worker_results),
        "workers": worker_results,
        "timed_out": any(worker.get("timed_out") for worker in worker_results),
        "exit_code": max((worker.get("exit_code", 0) for worker in worker_results), default=0),
    }
    for key in AGGREGATE_COUNTERS:
        if any(key in worker for worker in worker_results):
            result[key] = sum(int(worker.get(key, 0)) for worker in worker_results)
    if any("errors" in worker for worker in worker_results):
        errors = {}
        for worker in worker_results:
            for key, value in worker.get("errors", {}).items():
                errors[key] = errors.get(key, 0) + int(value)
        result["errors"] = errors
    elapsed_values = [worker.get("elapsed_s") for worker in worker_results if worker.get("elapsed_s") is not None]
    if elapsed_values:
        result["elapsed_s"] = max(elapsed_values)
    if any("trades_s" in worker for worker in worker_results):
        result["trades_s"] = round(sum(float(worker.get("trades_s", 0.0)) for worker in worker_results), 1)
    latency = aggregate_latency(worker_results)
    if latency:
        result["latency_avg_ms"] = latency
    return result


def result_failed(result):
    return bool(result.get("timed_out")) or int(result.get("exit_code", 0) or 0) != 0


def format_errors(errors):
    if not errors:
        return ""
    return ", ".join(f"{key}={value}" for key, value in sorted(errors.items()))


def run_stage_workers(
    stage_name,
    args,
    stem,
    config,
    command,
    overrides,
    pprof_enabled,
    pprof_profile,
    pprof_log,
    pprof_command,
):
    worker_count = int(args.workers)
    log_glob = Path(args.log_dir) / f"{stem}-w*.log"
    worker_specs = []
    for worker_index in range(worker_count):
        worker_overrides = worker_env(overrides, worker_index, worker_count)
        worker_specs.append(
            {
                "worker_index": worker_index,
                "log": str(Path(args.log_dir) / f"{stem}-w{worker_index}.log"),
                "command": command,
                "env": worker_overrides,
            }
        )

    if args.dry_run:
        result = {
            "stage": stage_name,
            "description": STAGES[stage_name]["description"],
            "target": float(args.target),
            "log": str(log_glob),
            "command": command,
            "env": overrides,
            "worker_count": worker_count,
            "workers": worker_specs,
            "dry_run": True,
        }
        if pprof_enabled:
            result.update(
                {
                    "pprof_profile": str(pprof_profile),
                    "pprof_log": str(pprof_log),
                    "pprof_command": pprof_command,
                }
            )
        return result

    Path(args.log_dir).mkdir(parents=True, exist_ok=True)
    processes = []
    threads = []
    codes = {}
    timed_out = False
    pprof_process = None
    pprof_output = None
    pprof_exit_code = None
    pprof_lock = threading.Lock()

    def maybe_start_pprof():
        nonlocal pprof_process, pprof_output
        if not pprof_enabled or pprof_process is not None:
            return
        with pprof_lock:
            if pprof_process is not None:
                return
            pprof_log.parent.mkdir(parents=True, exist_ok=True)
            pprof_output = pprof_log.open("w", encoding="utf-8")
            pprof_process = subprocess.Popen(
                pprof_command,
                cwd=args.cwd,
                stdout=pprof_output,
                stderr=subprocess.STDOUT,
                text=True,
            )

    def pump_output(process, log_path):
        with log_path.open("w", encoding="utf-8") as log_file:
            assert process.stdout is not None
            for line in process.stdout:
                log_file.write(line)
                if line.startswith("ready "):
                    maybe_start_pprof()

    for spec in worker_specs:
        env = os.environ.copy()
        env.update(spec["env"])
        process = subprocess.Popen(
            command,
            cwd=args.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        log_path = Path(spec["log"])
        thread = threading.Thread(target=pump_output, args=(process, log_path))
        thread.start()
        processes.append((spec["worker_index"], process, log_path))
        threads.append(thread)

    def kill_on_timeout():
        nonlocal timed_out
        alive = [process for _, process, _ in processes if process.poll() is None]
        if alive:
            timed_out = True
            for process in alive:
                process.kill()

    timeout = args.stage_timeout or max(args.duration + 180.0, args.duration * 3.0)
    timer = threading.Timer(timeout, kill_on_timeout)
    timer.start()
    try:
        for worker_index, process, _ in processes:
            codes[worker_index] = process.wait()
    finally:
        timer.cancel()
        for thread in threads:
            thread.join()

    if pprof_process is not None:
        try:
            pprof_exit_code = pprof_process.wait(timeout=args.pprof_seconds + 45.0)
        except subprocess.TimeoutExpired:
            pprof_process.kill()
            pprof_exit_code = pprof_process.wait()
        if pprof_output is not None:
            pprof_output.close()

    worker_results = []
    for worker_index, _, log_path in processes:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        result = parse_done_text(text) or {}
        result.update(
            {
                "worker_index": worker_index,
                "log": str(log_path),
                "exit_code": codes.get(worker_index, 1),
                "timed_out": timed_out,
            }
        )
        worker_results.append(result)

    result = aggregate_worker_results(stage_name, args.target, worker_results)
    result.update(
        {
            "description": STAGES[stage_name]["description"],
            "log": str(log_glob),
            "command": command,
            "env": overrides,
        }
    )
    if pprof_enabled:
        result.update(
            {
                "pprof_profile": str(pprof_profile),
                "pprof_log": str(pprof_log),
                "pprof_command": pprof_command,
                "pprof_started": pprof_process is not None,
                "pprof_exit_code": pprof_exit_code,
            }
        )
    return result


def run_stage(stage_name, args, timestamp):
    env = os.environ.copy()
    overrides = stage_env(stage_name)
    overrides.update(parse_env_overrides(args.env))
    env.update(overrides)
    stem = stage_log_stem(stage_name, timestamp, args.target, bool(args.target_sweep))
    config = prepare_stage_config(args.config, args.fresh_keystore_dir, stem, write=not args.dry_run)
    command = build_command(args.python, config, args.target, args.duration)
    log_path = Path(args.log_dir) / f"{stem}.log"
    pprof_enabled = stage_should_profile(args, stage_name)
    pprof_profile = Path(args.log_dir) / f"{stem}.pprof.pb.gz"
    pprof_log = Path(args.log_dir) / f"{stem}.pprof.log"
    pprof_command = build_pprof_command(args.pprof_url, args.pprof_seconds, pprof_profile) if pprof_enabled else None
    worker_count = int(getattr(args, "workers", 1) or 1)
    if worker_count <= 0:
        raise ValueError("workers must be positive")
    if worker_count > 1:
        return run_stage_workers(
            stage_name,
            args,
            stem,
            config,
            command,
            overrides,
            pprof_enabled,
            pprof_profile,
            pprof_log,
            pprof_command,
        )

    if args.dry_run:
        result = {
            "stage": stage_name,
            "description": STAGES[stage_name]["description"],
            "target": float(args.target),
            "log": str(log_path),
            "command": command,
            "env": overrides,
            "dry_run": True,
        }
        if pprof_enabled:
            result.update(
                {
                    "pprof_profile": str(pprof_profile),
                    "pprof_log": str(pprof_log),
                    "pprof_command": pprof_command,
                }
            )
        return result

    log_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    pprof_process = None
    pprof_output = None
    pprof_exit_code = None
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=args.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def kill_on_timeout():
            nonlocal timed_out
            if process.poll() is None:
                timed_out = True
                process.kill()

        timeout = args.stage_timeout or max(args.duration + 180.0, args.duration * 3.0)
        timer = threading.Timer(timeout, kill_on_timeout)
        timer.start()
        assert process.stdout is not None
        try:
            for line in process.stdout:
                print(line, end="")
                log_file.write(line)
                if pprof_enabled and pprof_process is None and line.startswith("ready "):
                    pprof_log.parent.mkdir(parents=True, exist_ok=True)
                    pprof_output = pprof_log.open("w", encoding="utf-8")
                    pprof_process = subprocess.Popen(
                        pprof_command,
                        cwd=args.cwd,
                        stdout=pprof_output,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
            code = process.wait()
        finally:
            timer.cancel()
            if pprof_process is not None:
                try:
                    pprof_exit_code = pprof_process.wait(timeout=args.pprof_seconds + 45.0)
                except subprocess.TimeoutExpired:
                    pprof_process.kill()
                    pprof_exit_code = pprof_process.wait()
                if pprof_output is not None:
                    pprof_output.close()

    text = log_path.read_text(encoding="utf-8", errors="replace")
    result = parse_done_text(text) or {}
    result.update(
        {
            "stage": stage_name,
            "description": STAGES[stage_name]["description"],
            "target": float(args.target),
            "log": str(log_path),
            "exit_code": code,
            "timed_out": timed_out,
        }
    )
    if pprof_enabled:
        result.update(
            {
                "pprof_profile": str(pprof_profile),
                "pprof_log": str(pprof_log),
                "pprof_command": pprof_command,
                "pprof_started": pprof_process is not None,
                "pprof_exit_code": pprof_exit_code,
            }
        )
    return result


def write_summary(results, path):
    lines = [
        "# Perf Stage Summary",
        "",
        "| Stage | Target | Workers | TPS | Fills | Taker Sent | Taker Submit | Maker Submit | Taker ms | Taker sign ms | Wait ms | Errors | Log | Profile |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for result in results:
        lat = result.get("latency_avg_ms", {})
        lines.append(
            "| {stage} | {target} | {workers} | {tps} | {fills} | {taker_sent} | {taker} | {maker} | {taker_ms} | {sign_ms} | {wait_ms} | {errors} | {log} | {profile} |".format(
                stage=result.get("stage", ""),
                target=result.get("target", ""),
                workers=result.get("worker_count", 1),
                tps=result.get("trades_s", ""),
                fills=result.get("fills", ""),
                taker_sent=result.get("taker_sent", ""),
                taker=result.get("taker_submit_ok", ""),
                maker=result.get("maker_submit_ok", ""),
                taker_ms=lat.get("taker", ""),
                sign_ms=lat.get("taker_sign", ""),
                wait_ms=lat.get("taker_inflight_wait", ""),
                errors=format_errors(result.get("errors", {})),
                log=result.get("log", ""),
                profile=result.get("pprof_profile", ""),
            )
        )
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.perf.json")
    parser.add_argument("--target", type=float, default=300)
    parser.add_argument("--target-sweep", default="")
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--stages", default="baseline,wide_accounts,time_inflight2,maker_guard,maker_adaptive,final_poll")
    parser.add_argument("--log-dir", default="/tmp")
    parser.add_argument("--summary")
    parser.add_argument("--fresh-keystore-dir", default="")
    parser.add_argument("--env", action="append", default=[], help="Override match.py environment as KEY=VALUE")
    parser.add_argument("--python", default=".venv/bin/python")
    parser.add_argument("--cwd", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--stage-timeout", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--pprof-url", default="")
    parser.add_argument("--pprof-seconds", type=float, default=20.0)
    parser.add_argument("--pprof-stages", default="baseline")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    stages = [stage.strip() for stage in args.stages.split(",") if stage.strip()]
    unknown = [stage for stage in stages if stage not in STAGES]
    if unknown:
        raise SystemExit(f"unknown stages: {', '.join(unknown)}")

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    targets = parse_target_sweep(args.target, args.target_sweep)
    results = []
    for target in targets:
        run_args = argparse.Namespace(**vars(args))
        run_args.target = target
        for stage in stages:
            results.append(run_stage(stage, run_args, timestamp))
    summary = args.summary or str(Path(args.log_dir) / f"perf-stage-summary-{timestamp}.md")
    if not args.dry_run:
        write_summary(results, summary)
        print(f"summary: {summary}")
    else:
        for result in results:
            if result.get("worker_count", 1) > 1:
                for worker in result["workers"]:
                    env = " ".join(f"{key}={value}" for key, value in worker["env"].items())
                    command = " ".join(worker["command"])
                    print(
                        f"[dry-run] {result['stage']} target={result['target']} "
                        f"worker={worker['worker_index']}/{result['worker_count']}: "
                        f"{env} {command} > {worker['log']}"
                    )
            else:
                env = " ".join(f"{key}={value}" for key, value in result["env"].items())
                command = " ".join(result["command"])
                print(f"[dry-run] {result['stage']} target={result['target']}: {env} {command} > {result['log']}")
    return 1 if any(result_failed(result) for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

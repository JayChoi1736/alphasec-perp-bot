import ast
import asyncio
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from dex import BUY, SELL
from match import (
    account_pair_count,
    apply_local_fill,
    active_role_names,
    choose_direction,
    has_book_liquidity,
    initial_taker_direction,
    latency_bucket,
    latency_summary,
    maker_size_after_error,
    maker_cooldown_until,
    maker_loop,
    market_depth,
    mode_enabled,
    normalize_position_poll_mode,
    record_position_update,
    position_poll_items,
    next_time_nonce_value,
    position_size_from_positions,
    record_latency,
    role_account_counts,
    role_deposit_targets,
    stagger_delay,
    summary_counts,
    submit_success_counter,
    tx_hex,
    worker_bounds,
)


class MatchHelperTest(unittest.TestCase):
    def test_worker_bounds_split_accounts_without_overlap(self):
        self.assertEqual(worker_bounds(100, 0, 4), (0, 25))
        self.assertEqual(worker_bounds(100, 1, 4), (25, 50))
        self.assertEqual(worker_bounds(100, 2, 4), (50, 75))
        self.assertEqual(worker_bounds(100, 3, 4), (75, 100))

    def test_worker_bounds_distribute_remainder_to_early_workers(self):
        self.assertEqual(worker_bounds(10, 0, 3), (0, 4))
        self.assertEqual(worker_bounds(10, 1, 3), (4, 7))
        self.assertEqual(worker_bounds(10, 2, 3), (7, 10))

    def test_account_pair_count_uses_target_over_per_account_rate_by_default(self):
        self.assertEqual(account_pair_count(300, 8), 38)

    def test_account_pair_count_allows_explicit_wide_account_fanout(self):
        self.assertEqual(account_pair_count(300, 8, override=300), 300)

    def test_account_pair_count_rejects_non_positive_override(self):
        with self.assertRaises(ValueError):
            account_pair_count(300, 8, override=0)

    def test_role_account_counts_keep_legacy_total_pair_override(self):
        self.assertEqual(
            role_account_counts(300, 8, total_override=120),
            (120, 120),
        )

    def test_role_account_counts_allow_taker_fanout_without_extra_makers(self):
        self.assertEqual(
            role_account_counts(300, 8, maker_override=40, taker_override=300),
            (40, 300),
        )

    def test_role_deposit_targets_use_default_for_both_roles(self):
        self.assertEqual(role_deposit_targets(100), (100.0, 100.0))

    def test_role_deposit_targets_allow_maker_only_large_liquidity(self):
        self.assertEqual(role_deposit_targets(100, maker_override=2000), (2000.0, 100.0))

    def test_choose_direction_flips_at_inventory_cap(self):
        self.assertEqual(choose_direction(0.10, 0.10, BUY), SELL)
        self.assertEqual(choose_direction(-0.10, 0.10, SELL), BUY)
        self.assertEqual(choose_direction(0.00, 0.10, SELL), SELL)

    def test_initial_taker_direction_reduces_existing_inventory(self):
        self.assertEqual(initial_taker_direction(0.01, 0), SELL)
        self.assertEqual(initial_taker_direction(-0.01, 0), BUY)

    def test_initial_taker_direction_alternates_when_flat(self):
        self.assertEqual(initial_taker_direction(0.0, 0), BUY)
        self.assertEqual(initial_taker_direction(0.0, 1), SELL)

    def test_mode_enabled_accepts_common_disabled_values(self):
        self.assertFalse(mode_enabled("off"))
        self.assertFalse(mode_enabled("false"))
        self.assertFalse(mode_enabled("0"))
        self.assertTrue(mode_enabled("on"))

    def test_active_role_names_excludes_disabled_roles(self):
        self.assertEqual(active_role_names("refresh", "on"), ("maker", "taker"))
        self.assertEqual(active_role_names("off", "on"), ("taker",))
        self.assertEqual(active_role_names("refresh", "off"), ("maker",))

    def test_stagger_delay_evenly_spreads_workers_inside_interval(self):
        self.assertEqual(stagger_delay(0, 4, 2.0), 0.0)
        self.assertEqual(stagger_delay(1, 4, 2.0), 0.5)
        self.assertEqual(stagger_delay(2, 4, 2.0), 1.0)
        self.assertEqual(stagger_delay(3, 4, 2.0), 1.5)

    def test_stagger_delay_is_zero_when_disabled_or_single_worker(self):
        self.assertEqual(stagger_delay(0, 1, 2.0), 0.0)
        self.assertEqual(stagger_delay(3, 4, 0.0), 0.0)

    def test_apply_local_fill_tracks_expected_taker_inventory(self):
        self.assertEqual(apply_local_fill(0.00, BUY, 0.001), 0.001)
        self.assertEqual(apply_local_fill(0.00, SELL, 0.001), -0.001)

    def test_record_position_update_accumulates_absolute_delta(self):
        pos = [0.0, 0.0]
        prev = [0.0, 0.0]
        fills = [0.0]

        record_position_update(pos, prev, fills, 1, 0.003)
        record_position_update(pos, prev, fills, 1, -0.001)

        self.assertEqual(pos[1], -0.001)
        self.assertEqual(prev[1], -0.001)
        self.assertEqual(fills[0], 0.007)

    def test_normalize_position_poll_mode_accepts_aliases(self):
        self.assertEqual(normalize_position_poll_mode("continuous"), "continuous")
        self.assertEqual(normalize_position_poll_mode("on"), "continuous")
        self.assertEqual(normalize_position_poll_mode("final"), "final")
        self.assertEqual(normalize_position_poll_mode("end"), "final")
        self.assertEqual(normalize_position_poll_mode("off"), "off")

    def test_normalize_position_poll_mode_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            normalize_position_poll_mode("sometimes")

    def test_position_poll_items_skip_maker_accounts(self):
        self.assertEqual(position_poll_items(["m0", "m1", "t0", "t1"], 2), [(2, "t0"), (3, "t1")])

    def test_tx_hex_keeps_single_prefix(self):
        self.assertEqual(tx_hex(bytes.fromhex("abcd")), "0xabcd")

    def test_submit_success_counter_names_role_from_error_prefix(self):
        self.assertEqual(submit_success_counter("taker_error"), "taker_submit_ok")
        self.assertEqual(submit_success_counter("maker_error"), "maker_submit_ok")

    def test_summary_counts_exposes_role_submit_totals(self):
        stats = Counter(
            {
                "submit_ok": 10,
                "taker_submit_ok": 4,
                "maker_submit_ok": 6,
                "taker_sent": 5,
                "cancel_ok": 2,
            }
        )
        self.assertEqual(
            summary_counts(stats),
            {
                "submit_ok": 10,
                "taker_submit_ok": 4,
                "maker_submit_ok": 6,
                "taker_sent": 5,
                "cancel_ok": 2,
                "maker_cooldown": 0,
                "maker_cooldown_skipped": 0,
            },
        )

    def test_position_size_from_positions_returns_matching_market_size(self):
        positions = [
            {"marketId": "2", "size": "3000000000000000000"},
            {"marketId": "1", "size": "-1500000000000000000"},
        ]
        self.assertEqual(position_size_from_positions(positions, 1), -1.5)

    def test_position_size_from_positions_returns_zero_when_absent(self):
        self.assertEqual(position_size_from_positions([], 1), 0.0)

    def test_latency_bucket_groups_elapsed_seconds(self):
        self.assertEqual(latency_bucket(0.009), "lt_10ms")
        self.assertEqual(latency_bucket(0.050), "lt_100ms")
        self.assertEqual(latency_bucket(1.500), "lt_2000ms")
        self.assertEqual(latency_bucket(2.500), "ge_2000ms")

    def test_record_latency_summarizes_count_average_and_buckets(self):
        stats = Counter()
        record_latency(stats, "taker", 0.100)
        record_latency(stats, "taker", 0.300)

        self.assertEqual(
            latency_summary(stats, "taker"),
            {
                "count": 2,
                "avg_ms": 200.0,
                "buckets": {"lt_250ms": 1, "lt_500ms": 1},
            },
        )

    def test_market_depth_sums_matching_market_levels(self):
        data = [
            {
                "symbol": "1",
                "bids": [["100", "0.2"], ["99", "0.1"]],
                "asks": [["101", "0.3"], ["102", "0.1"]],
            }
        ]

        self.assertEqual(market_depth(data, 1), (0.30000000000000004, 0.4))

    def test_has_book_liquidity_requires_both_sides(self):
        data = [
            {
                "marketId": 1,
                "bids": [["100", "0.2"]],
                "asks": [["101", "0.1"]],
            }
        ]

        self.assertTrue(has_book_liquidity(data, 1, 0.1, 0.1))
        self.assertFalse(has_book_liquidity(data, 1, 0.3, 0.1))
        self.assertFalse(has_book_liquidity(data, 1, 0.1, 0.2))

    def test_maker_size_after_error_backs_off_insufficient_margin(self):
        self.assertEqual(
            maker_size_after_error("insufficient_margin", 0.005, min_size=0.001, backoff=0.5),
            0.0025,
        )

    def test_maker_size_after_error_respects_minimum(self):
        self.assertEqual(
            maker_size_after_error("insufficient_margin", 0.0015, min_size=0.001, backoff=0.5),
            0.001,
        )

    def test_maker_size_after_error_ignores_other_errors(self):
        self.assertEqual(
            maker_size_after_error("nonce", 0.005, min_size=0.001, backoff=0.5),
            0.005,
        )

    def test_maker_cooldown_until_requires_insufficient_margin_threshold(self):
        self.assertEqual(
            maker_cooldown_until(
                "nonce",
                consecutive_errors=3,
                threshold=2,
                cooldown_sec=10.0,
                now=100.0,
            ),
            0.0,
        )
        self.assertEqual(
            maker_cooldown_until(
                "insufficient_margin",
                consecutive_errors=1,
                threshold=2,
                cooldown_sec=10.0,
                now=100.0,
            ),
            0.0,
        )
        self.assertEqual(
            maker_cooldown_until(
                "insufficient_margin",
                consecutive_errors=2,
                threshold=2,
                cooldown_sec=10.0,
                now=100.0,
            ),
            110.0,
        )

    def test_summary_counts_exposes_maker_cooldown_totals(self):
        stats = Counter({"maker_cooldown": 2, "maker_cooldown_skipped": 3})

        self.assertEqual(summary_counts(stats)["maker_cooldown"], 2)
        self.assertEqual(summary_counts(stats)["maker_cooldown_skipped"], 3)

    def test_taker_loop_call_uses_declared_positional_arity(self):
        tree = ast.parse(Path("match.py").read_text())
        taker_loop_arity = None
        taker_loop_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "taker_loop":
                taker_loop_arity = len(node.args.args)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "taker_loop":
                taker_loop_calls.append(len(node.args))

        self.assertIsNotNone(taker_loop_arity)
        self.assertEqual(taker_loop_calls, [taker_loop_arity])

    def test_maker_loop_call_uses_declared_positional_arity(self):
        tree = ast.parse(Path("match.py").read_text())
        maker_loop_arity = None
        maker_loop_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "maker_loop":
                maker_loop_arity = len(node.args.args)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "maker_loop":
                maker_loop_calls.append(len(node.args))

        self.assertIsNotNone(maker_loop_arity)
        self.assertEqual(maker_loop_calls, [maker_loop_arity])

    def test_next_time_nonce_uses_now_when_it_is_highest(self):
        self.assertEqual(next_time_nonce_value(previous=1000, now_ms=2000, state_nonce=1500), 2000)

    def test_next_time_nonce_is_monotonic_when_called_with_same_millisecond(self):
        self.assertEqual(next_time_nonce_value(previous=2000, now_ms=2000, state_nonce=1500), 2001)

    def test_next_time_nonce_stays_above_state_nonce(self):
        self.assertEqual(next_time_nonce_value(previous=0, now_ms=2000, state_nonce=2500), 2501)


class MakerLoopCooldownTest(unittest.IsolatedAsyncioTestCase):
    async def test_maker_loop_cools_down_after_margin_error(self):
        stats = Counter()
        samples = {}
        stop = asyncio.Event()

        async def fail_post(*args, **kwargs):
            raise RuntimeError("insufficient margin for perp order")

        with patch("match.post_maker_liquidity", fail_post):
            task = asyncio.create_task(
                maker_loop(
                    rpc=None,
                    account=object(),
                    market_id=1,
                    ask_prices=[1.0],
                    bid_prices=[1.0],
                    size=0.005,
                    interval=0.01,
                    cancel_every=0,
                    initial_delay=0.0,
                    guard_state=None,
                    nonce_mode="time",
                    stop=stop,
                    stats=stats,
                    samples=samples,
                    error_cooldown_threshold=1,
                    error_cooldown_sec=0.03,
                )
            )
            await asyncio.sleep(0.07)
            stop.set()
            await task

        self.assertGreaterEqual(stats["maker_cooldown"], 1)
        self.assertGreaterEqual(stats["maker_cooldown_skipped"], 1)


if __name__ == "__main__":
    unittest.main()

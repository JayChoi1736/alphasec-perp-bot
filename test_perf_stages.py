import json
import unittest
from tempfile import TemporaryDirectory

from perf_stages import (
    build_pprof_command,
    parse_args,
    parse_done_line,
    parse_done_text,
    parse_env_overrides,
    prepare_stage_config,
    parse_target_sweep,
    pprof_profile_url,
    run_stage,
    stage_log_stem,
    stage_env,
    stage_should_profile,
    write_summary,
)


class PerfStagesTest(unittest.TestCase):
    def test_parse_done_line_extracts_rates_counters_and_latency_averages(self):
        line = (
            "DONE: 717 fills in 21s = 34.1 trades/s; "
            "submit_ok=1829 taker_submit_ok=750 maker_submit_ok=1079 taker_sent=788 "
            "cancel_ok=38 maker_refresh_skipped=0 book_guard_ok=0 "
            "errors={} "
            "latency_avg_ms={'taker': 971.7, 'maker': 666.6, 'taker_sign': 2.5, "
            "'taker_inflight_wait': 1006.8} "
            "latency={'taker': {'count': 750}} samples={}"
        )

        parsed = parse_done_line(line)

        self.assertEqual(parsed["fills"], 717)
        self.assertEqual(parsed["elapsed_s"], 21)
        self.assertEqual(parsed["trades_s"], 34.1)
        self.assertEqual(parsed["submit_ok"], 1829)
        self.assertEqual(parsed["taker_submit_ok"], 750)
        self.assertEqual(parsed["maker_submit_ok"], 1079)
        self.assertEqual(parsed["latency_avg_ms"]["taker"], 971.7)
        self.assertEqual(parsed["latency_avg_ms"]["taker_inflight_wait"], 1006.8)

    def test_parse_done_text_uses_last_done_line(self):
        text = "\n".join(
            [
                "DONE: 1 fills in 1s = 1.0 trades/s; submit_ok=1",
                "noise",
                "DONE: 2 fills in 1s = 2.0 trades/s; submit_ok=2",
            ]
        )

        self.assertEqual(parse_done_text(text)["fills"], 2)

    def test_wide_accounts_stage_increases_taker_fanout_without_inflight_nonce_risk(self):
        env = stage_env("wide_accounts")

        self.assertEqual(env["MATCH_TAKER_COUNT"], "300")
        self.assertEqual(env["MATCH_MAKER_COUNT"], "40")
        self.assertEqual(env["MATCH_PER_ACCOUNT_TPS"], "1")
        self.assertEqual(env["MATCH_ACCOUNT_INFLIGHT"], "1")

    def test_parse_done_text_returns_none_when_missing_done_line(self):
        self.assertIsNone(parse_done_text("no done line"))

    def test_parse_env_overrides_accepts_key_value_pairs(self):
        self.assertEqual(
            parse_env_overrides(["MATCH_GAS_ETH=0.0005", "MATCH_MAKER_SIZE=0.001"]),
            {"MATCH_GAS_ETH": "0.0005", "MATCH_MAKER_SIZE": "0.001"},
        )

    def test_parse_env_overrides_rejects_missing_separator(self):
        with self.assertRaises(ValueError):
            parse_env_overrides(["MATCH_GAS_ETH"])

    def test_parse_args_accepts_stage_timeout(self):
        args = parse_args(["--stage-timeout", "12.5"])

        self.assertEqual(args.stage_timeout, 12.5)

    def test_parse_target_sweep_uses_default_target_when_empty(self):
        self.assertEqual(parse_target_sweep(300, ""), [300.0])

    def test_parse_target_sweep_accepts_comma_separated_values(self):
        self.assertEqual(parse_target_sweep(300, "120,180.5,240"), [120.0, 180.5, 240.0])

    def test_parse_target_sweep_rejects_non_positive_values(self):
        with self.assertRaises(ValueError):
            parse_target_sweep(300, "120,0")

    def test_stage_log_stem_adds_target_suffix_only_for_sweep(self):
        self.assertEqual(stage_log_stem("baseline", "20260630-010203", 180.5, False), "perf-stage-baseline-20260630-010203")
        self.assertEqual(stage_log_stem("baseline", "20260630-010203", 180.5, True), "perf-stage-baseline-t180p5-20260630-010203")

    def test_pprof_profile_url_adds_seconds_query(self):
        url = pprof_profile_url("https://l2-pprof-perf.dexor.trade/debug/pprof/profile", 20)

        self.assertEqual(url, "https://l2-pprof-perf.dexor.trade/debug/pprof/profile?seconds=20")

    def test_pprof_profile_url_replaces_existing_seconds_query(self):
        url = pprof_profile_url("https://host/debug/pprof/profile?seconds=1&debug=0", 30)

        self.assertEqual(url, "https://host/debug/pprof/profile?seconds=30&debug=0")

    def test_stage_should_profile_accepts_named_stage_or_all(self):
        args = parse_args(["--pprof-url", "https://host/debug/pprof/profile", "--pprof-stages", "baseline"])
        self.assertTrue(stage_should_profile(args, "baseline"))
        self.assertFalse(stage_should_profile(args, "wide_accounts"))

        args = parse_args(["--pprof-url", "https://host/debug/pprof/profile", "--pprof-stages", "all"])
        self.assertTrue(stage_should_profile(args, "wide_accounts"))

    def test_build_pprof_command_writes_raw_profile_with_curl(self):
        command = build_pprof_command(
            "https://host/debug/pprof/profile",
            seconds=20,
            output_path="/tmp/profile.pb.gz",
        )

        self.assertEqual(
            command,
            [
                "curl",
                "-fsSL",
                "--max-time",
                "50",
                "-o",
                "/tmp/profile.pb.gz",
                "https://host/debug/pprof/profile?seconds=20",
            ],
        )

    def test_run_stage_dry_run_includes_pprof_metadata(self):
        with TemporaryDirectory() as tmp:
            args = parse_args(
                [
                    "--dry-run",
                    "--log-dir",
                    tmp,
                    "--pprof-url",
                    "https://host/debug/pprof/profile",
                    "--pprof-stages",
                    "baseline",
                ]
            )

            result = run_stage("baseline", args, "20260630-010203")

        self.assertEqual(result["pprof_profile"], f"{tmp}/perf-stage-baseline-20260630-010203.pprof.pb.gz")
        self.assertEqual(
            result["pprof_command"],
            [
                "curl",
                "-fsSL",
                "--max-time",
                "50",
                "-o",
                f"{tmp}/perf-stage-baseline-20260630-010203.pprof.pb.gz",
                "https://host/debug/pprof/profile?seconds=20",
            ],
        )

    def test_run_stage_dry_run_uses_target_suffix_for_sweep(self):
        with TemporaryDirectory() as tmp:
            args = parse_args(["--dry-run", "--log-dir", tmp, "--target", "180", "--target-sweep", "120,180"])

            result = run_stage("baseline", args, "20260630-010203")

        self.assertEqual(result["target"], 180.0)
        self.assertEqual(result["log"], f"{tmp}/perf-stage-baseline-t180-20260630-010203.log")

    def test_write_summary_includes_profile_path_when_present(self):
        with TemporaryDirectory() as tmp:
            path = f"{tmp}/summary.md"
            write_summary(
                [
                    {
                        "stage": "baseline",
                        "target": 300.0,
                        "trades_s": 123.4,
                        "fills": 100,
                        "taker_submit_ok": 90,
                        "maker_submit_ok": 80,
                        "latency_avg_ms": {"taker": 1.1, "taker_sign": 2.2, "taker_inflight_wait": 3.3},
                        "log": "/tmp/load.log",
                        "pprof_profile": "/tmp/profile.pb.gz",
                    }
                ],
                path,
            )
            with open(path, encoding="utf-8") as handle:
                text = handle.read()

        self.assertIn("| Target |", text)
        self.assertIn("| Profile |", text)
        self.assertIn("| baseline | 300.0 |", text)
        self.assertIn("/tmp/profile.pb.gz", text)


    def test_prepare_stage_config_writes_fresh_keystore_config(self):
        with TemporaryDirectory() as tmp:
            base_config = f"{tmp}/config.perf.json"
            fresh_dir = f"{tmp}/fresh"
            with open(base_config, "w", encoding="utf-8") as handle:
                json.dump({"rpc_url": "https://rpc", "keystore": "accounts.perf.json"}, handle)

            prepared = prepare_stage_config(base_config, fresh_dir, "perf-stage-baseline-ts", write=True)

            self.assertEqual(prepared, f"{fresh_dir}/perf-stage-baseline-ts.config.json")
            with open(prepared, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["rpc_url"], "https://rpc")
            self.assertEqual(data["keystore"], f"{fresh_dir}/perf-stage-baseline-ts.accounts.json")


if __name__ == "__main__":
    unittest.main()

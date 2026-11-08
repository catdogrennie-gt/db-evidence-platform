import unittest
from datetime import datetime, timezone, timedelta
from platform_app.metrics_derivation import derive, derive_digest_window, source_is_fresh


def frame(t=30, **updates):
    value = dict(
        instance_id="i",
        server_uuid="u",
        owner_collector="identity_status",
        collector_boot_id="boot",
        counter_epoch=1,
        observed_monotonic_ns=t * 1_000_000_000,
        observed_at=(datetime(2026, 11, 8, tzinfo=timezone.utc) + timedelta(seconds=t)).isoformat(),
        uptime_seconds=100 + t,
        raw_values={
            "Questions": 100 + t * 2,
            "Com_commit": 10 + t,
            "Com_rollback": 0,
            "Innodb_buffer_pool_read_requests": 1000 + t * 10,
            "Innodb_buffer_pool_reads": 10 + t,
            "Threads_connected": 4,
            "Threads_running": 1,
        },
    )
    value.update(updates)
    return value


class MetricsTests(unittest.TestCase):
    def test_rates_use_actual_observations_and_explicit_transaction_label(self):
        result = derive(frame(0), frame(30))
        self.assertEqual(result["values"]["qps"], 2)
        self.assertEqual(result["values"]["explicit_commit_rollback_per_second"], 1)
        self.assertAlmostEqual(result["values"]["buffer_pool_hit_ratio"], 0.9)
        self.assertNotIn("tps", result["values"])

    def test_invalid_epochs_and_intervals_are_unknown_not_zero(self):
        for prior, current, reason in [
            (None, frame(), "first_sample"),
            (frame(0), frame(30, server_uuid="new"), "identity_changed"),
            (frame(0), frame(30, collector_boot_id="new"), "collector_restarted"),
            (frame(0), frame(30, counter_epoch=2), "counter_epoch_changed"),
            (frame(0), frame(30, uptime_seconds=1), "server_restarted"),
            (frame(0), frame(90), "invalid_interval"),
        ]:
            with self.subTest(reason=reason):
                result = derive(prior, current)
                self.assertIsNone(result["values"]["qps"])
                self.assertIn(reason, result["reasons"])

    def test_counter_reset_only_invalidates_rates_gauge_remains_observed(self):
        current = frame(30)
        current["raw_values"]["Questions"] = 1
        result = derive(frame(0), current)
        self.assertIsNone(result["values"]["qps"])
        self.assertEqual(result["values"]["connections"], 4)
        self.assertIn("counter_decreased", result["reasons"])

    def test_idle_buffer_ratio_does_not_invalidate_qps_window(self):
        previous = frame(0)
        current = frame(30)
        current["raw_values"]["Innodb_buffer_pool_read_requests"] = previous["raw_values"][
            "Innodb_buffer_pool_read_requests"
        ]
        current["raw_values"]["Innodb_buffer_pool_reads"] = previous["raw_values"][
            "Innodb_buffer_pool_reads"
        ]
        value = derive(previous, current)
        self.assertEqual(value["window_validity"], "valid")
        self.assertEqual(value["metric_validity"]["qps"]["validity"], "valid")
        self.assertEqual(
            value["metric_validity"]["buffer_pool_hit_ratio"]["reasons"], ["zero_denominator"]
        )
        self.assertIsNone(value["values"]["buffer_pool_hit_ratio"])

    def test_source_freshness_does_not_borrow_database_success(self):
        self.assertFalse(
            source_is_fresh(
                {"availability": "unreachable", "observed_at": "2026-11-08T00:00:00Z"},
                "2026-11-08T00:00:01Z",
                30,
            )
        )
        self.assertFalse(
            source_is_fresh(
                {"availability": "available", "observed_at": "2026-11-08T00:00:00Z"},
                "2026-11-08T00:00:46Z",
                30,
            )
        )
        self.assertTrue(
            source_is_fresh(
                {"availability": "available", "observed_at": "2026-11-08T00:00:00Z"},
                "2026-11-08T00:01:00Z",
                60,
            )
        )

    def test_restart_with_new_uptime_above_previous_still_breaks_epoch(self):
        previous = frame(0, uptime_seconds=10)
        current = frame(30, uptime_seconds=20)
        value = derive(previous, current)
        self.assertIsNone(value["values"]["qps"])
        self.assertIn("uptime_epoch_changed", value["reasons"])

    def test_digest_has_its_own_sixty_second_window_and_new_digest_unknown(self):
        previous = frame(
            0,
            owner_collector="statement_digest",
            raw_values={
                "rows": [
                    {
                        "SCHEMA_NAME": "s",
                        "DIGEST": "d",
                        "COUNT_STAR": 2,
                        "SUM_TIMER_WAIT": 2_000_000_000_000,
                    }
                ]
            },
        )
        current = frame(
            90,
            owner_collector="statement_digest",
            raw_values={
                "rows": [
                    {
                        "SCHEMA_NAME": "s",
                        "DIGEST": "d",
                        "COUNT_STAR": 5,
                        "SUM_TIMER_WAIT": 8_000_000_000_000,
                    },
                    {"SCHEMA_NAME": "s", "DIGEST": "new", "COUNT_STAR": 1},
                ]
            },
        )
        result = derive_digest_window(previous, current, interval_seconds=60)
        self.assertEqual(result["rows"][0]["count_delta"], 3)
        self.assertEqual(result["rows"][0]["total_time_seconds_delta"], 6)
        self.assertIsNone(result["rows"][1]["count_delta"])

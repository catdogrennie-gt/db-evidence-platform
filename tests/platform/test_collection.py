import unittest
from unittest.mock import patch
from platform_app.connectors import ConnectorError, open_mysql
from platform_app.collectors.mysql import collect_mysql
from platform_app.collectors.host import parse_host_files, build_host_frame


class Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args=None):
        self.db.calls.append((sql, args))
        if "data_lock_waits" in sql:
            raise PermissionError("do not leak secret")
        if "@@server_uuid" in sql:
            self.rows = [
                {
                    "server_uuid": "u",
                    "version": "8.4.5",
                    "server_observed_at": "2026-11-08T00:00:00Z",
                    "performance_schema_enabled": getattr(self.db, "performance_schema_enabled", 1),
                }
            ]
        elif "global_status" in sql or "SHOW GLOBAL STATUS" in sql:
            self.rows = [
                {"Variable_name": "Uptime", "Value": "100"},
                {"Variable_name": "Questions", "Value": "12"},
                {"Variable_name": "Com_commit", "Value": "4"},
            ]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, **kwargs):
        self.calls = []
        self.closed = False
        self.kwargs = kwargs

    def cursor(self):
        return Cursor(self)

    def connect(self, **kwargs):
        pass

    def close(self):
        self.closed = True


class CollectionTests(unittest.TestCase):
    def test_direct_requires_verified_tls_and_never_leaks_credentials(self):
        with self.assertRaises(ConnectorError) as raised:
            with open_mysql(
                {"mode": "direct", "db_host": "example", "db_port": 3306},
                {"username": "u", "password": "SECRET"},
            ):
                pass
        self.assertEqual(raised.exception.code, "tls_required")
        self.assertNotIn("SECRET", str(raised.exception))

    def test_uuid_change_closes_session(self):
        db = FakeConnection()
        with (
            patch("platform_app.connectors.pymysql.Connection", return_value=db),
            patch("platform_app.connectors.ssl.create_default_context"),
            patch("platform_app.connectors.socket.create_connection"),
        ):
            with self.assertRaises(ConnectorError) as raised:
                with open_mysql(
                    {
                        "mode": "direct",
                        "db_host": "127.0.0.1",
                        "db_port": 3306,
                        "tls_mode": "disabled",
                        "expected_server_uuid": "other",
                    },
                    {"username": "u", "password": "p"},
                ):
                    pass
        self.assertEqual(raised.exception.code, "identity_mismatch")
        self.assertTrue(db.closed)

    def test_remote_plain_transport_is_rejected_even_when_explicit(self):
        with self.assertRaises(ConnectorError) as raised:
            with open_mysql(
                {"mode": "direct", "db_host": "192.0.2.20", "tls_mode": "disabled"},
                {"username": "u", "password": "p"},
            ):
                pass
        self.assertEqual(raised.exception.code, "tls_required")

    def test_isolated_plain_loopback_explicitly_disables_driver_opportunistic_tls(self):
        created = []

        def connection(**kwargs):
            value = FakeConnection(**kwargs)
            created.append(value)
            return value

        with (
            patch("platform_app.connectors.pymysql.Connection", side_effect=connection),
            patch("platform_app.connectors.socket.create_connection"),
        ):
            with open_mysql(
                {"mode": "direct", "db_host": "127.0.0.1", "tls_mode": "disabled"},
                {"username": "u", "password": "p"},
            ):
                pass
        self.assertTrue(created[0].kwargs.get("ssl_disabled", False))

    def test_runtime_failure_does_not_erase_transaction_or_session_observations(self):
        result = collect_mysql(
            FakeConnection(), {"instance_id": "i", "collector_boot_id": "b"}, "runtime"
        ).to_dict()
        sources = {item["source"]: item for item in result["evidence_items"]}
        self.assertEqual(sources["mysql.lock_waits"]["availability"], "permission_denied")
        self.assertEqual(sources["mysql.transactions"]["availability"], "available")
        self.assertEqual(sources["mysql.sessions"]["availability"], "available")
        self.assertNotIn("do not leak secret", str(result))

    def test_disabled_performance_schema_is_not_reported_as_zero_locks(self):
        db = FakeConnection()
        db.performance_schema_enabled = 0
        value = collect_mysql(db, {"instance_id": "i"}, "runtime").to_dict()
        lock = next(v for v in value["evidence_items"] if v["source"] == "mysql.lock_waits")
        self.assertEqual(lock["availability"], "not_configured")
        self.assertIsNone(value["metric_frame"]["raw_values"]["lock_waits"])

    def test_collection_unknown_profile_rejected_and_identity_observation_retained(self):
        with self.assertRaises(ValueError):
            collect_mysql(FakeConnection(), {}, "arbitrary sql")
        result = collect_mysql(
            FakeConnection(), {"instance_id": "i", "collector_boot_id": "b"}, "identity_status"
        ).to_dict()
        self.assertEqual(result["server_uuid"], "u")
        self.assertEqual(result["metric_frame"]["raw_values"]["Questions"], 12)
        self.assertEqual(result["metric_frame"]["raw_values"]["Com_commit"], 4)
        self.assertIsNone(result["metric_frame"]["derived_values"]["qps"])

    def test_host_memory_and_cpu_units_are_explicit(self):
        data = parse_host_files(
            {
                "/proc/stat": "cpu  10 2 8 80 5 1 1 0 0 0\n",
                "/proc/meminfo": "MemTotal: 1000 kB\nMemAvailable: 200 kB\n",
                "/proc/loadavg": "0.25 0.50 0.75 1/20 12\n",
                "/proc/uptime": "123.5 120\n",
                "/proc/diskstats": "8 0 sda 1 0 2 3 4 0 5 6 0 7 8\n",
                "/proc/sys/kernel/random/boot_id": "boot\n",
                "/etc/hostname": "test-host\n",
            }
        )
        self.assertEqual(data["memory_available_bytes"], 204800)
        self.assertEqual(data["cpu_total_ticks"], 107)
        self.assertEqual(data["cpu_idle_ticks"], 85)
        self.assertEqual(data["load1"], 0.25)
        self.assertEqual(data["disk_counters"][0]["sectors_written"], 5)

    def test_host_boot_uuid_is_source_epoch_and_numeric_counter_epoch_is_persistable(self):
        frame = build_host_frame(
            {"host_boot_id": "kernel-uuid", "uptime_seconds": 100},
            {"instance_id": "i", "server_uuid": "u", "counter_epoch": 3},
            "2026-11-08T00:00:00Z",
            1,
            "available",
        )
        self.assertEqual(frame["source_epoch"], "kernel-uuid")
        self.assertEqual(frame["counter_epoch"], 3)
        self.assertIsInstance(frame["counter_epoch"], int)

    def test_log_epoch_uses_current_uptime_and_resets_old_ring_watermark(self):
        anchor = {
            "server_uuid": "u",
            "collector_boot_id": "b",
            "counter_epoch": 7,
            "uptime_seconds": 1000,
            "observed_monotonic_ns": 1_000_000_000,
        }
        context = {
            "instance_id": "i",
            "collector_boot_id": "b",
            "identity_epoch_anchor": anchor,
            "error_log_cursor": {
                "source_epoch": "7",
                "last_server_event_at": "2026-11-08T00:00:00Z",
            },
        }
        from platform_app.collectors.logs import result

        with (
            patch(
                "platform_app.collectors.mysql.time.perf_counter_ns", return_value=31_000_000_000
            ),
            patch(
                "platform_app.collectors.mysql.collect_error_table",
                return_value=result("mysql.error_log", {"events": []}),
            ) as log,
        ):
            collect_mysql(FakeConnection(), context, "error_log")
        self.assertEqual(log.call_args.kwargs["source_epoch"], "8")
        self.assertIsNone(log.call_args.args[1])
        anchor["uptime_seconds"] = 70
        with (
            patch(
                "platform_app.collectors.mysql.time.perf_counter_ns", return_value=31_000_000_000
            ),
            patch(
                "platform_app.collectors.mysql.collect_error_table",
                return_value=result("mysql.error_log", {"events": []}),
            ) as log,
        ):
            collect_mysql(FakeConnection(), context, "error_log")
        self.assertEqual(log.call_args.kwargs["source_epoch"], "7")
        self.assertIs(log.call_args.args[1], context["error_log_cursor"])

    def test_column_metadata_uses_bounded_schema_selection_and_table_type(self):
        db = FakeConnection()
        value = collect_mysql(
            db, {"instance_id": "i", "selected_schemas": ["evidence_lab"]}, "schema_config"
        ).to_dict()
        sources = {item["source"]: item for item in value["evidence_items"]}
        self.assertIn("mysql.columns", sources)
        sql, params = next(
            (sql, params) for sql, params in db.calls if "information_schema.columns" in sql
        )
        self.assertEqual(params, ("evidence_lab", 0))
        self.assertIn("COLUMN_NAME,DATA_TYPE,ORDINAL_POSITION,IS_NULLABLE", sql)
        self.assertIn("LIMIT 2001 OFFSET %s", sql)
        self.assertIn(
            "TABLE_TYPE", next(sql for sql, _ in db.calls if "information_schema.tables" in sql)
        )
        self.assertEqual(sources["mysql.columns"]["coverage"]["selected_schemas"], ["evidence_lab"])

    def test_column_privilege_failure_is_explicit_and_does_not_hide_other_metadata(self):
        class ColumnDenied(Cursor):
            def execute(self, sql, args=None):
                if "information_schema.columns" in sql:
                    raise PermissionError("private diagnostic text")
                return super().execute(sql, args)

        db = FakeConnection()
        db.cursor = lambda: ColumnDenied(db)
        value = collect_mysql(db, {"instance_id": "i"}, "schema_config").to_dict()
        sources = {item["source"]: item for item in value["evidence_items"]}
        self.assertEqual(sources["mysql.columns"]["availability"], "permission_denied")
        self.assertEqual(sources["mysql.tables"]["availability"], "available")
        self.assertEqual(sources["mysql.indexes"]["availability"], "available")
        self.assertNotIn("private diagnostic text", str(value))

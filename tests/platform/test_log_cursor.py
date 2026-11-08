import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from platform_app.collectors.logs import (
    read_slow_file,
    redact_sql,
    collect_slow_table,
    collect_error_table,
)

RECORD = b"# Time: 2026-11-08T00:00:00.000000Z\n# User@Host: admin[admin] @ localhost [] Id: 7\n# Query_time: 1.500000 Lock_time: 0.000100 Rows_sent: 1 Rows_examined: 50\nSET timestamp=1794096000;\nSELECT * FROM t WHERE secret='PLACEHOLDER_SECRET' AND id=14;\n"


class LocalSFTP:
    def __init__(self, root):
        self.root = root

    def normalize(self, path):
        return str(Path(path).resolve()).replace("\\", "/")

    def stat(self, path):
        x = Path(path).stat()
        return SimpleNamespace(st_size=x.st_size, st_mtime=x.st_mtime)

    def open(self, path, mode="rb"):
        return open(path, mode)


class LogTests(unittest.TestCase):
    def test_error_buffer_repeated_events_keep_occurrences_and_poll_deduplicates(self):
        class DB:
            def cursor(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, args=None):
                if "MIN(LOGGED)" in sql:
                    self.rows = [
                        {"earliest": "2026-11-08T00:00:00Z", "latest": "2026-11-08T00:00:00Z"}
                    ]
                elif "global_status" in sql:
                    self.rows = []
                else:
                    self.rows = [
                        {
                            "LOGGED": "2026-11-08T00:00:00Z",
                            "THREAD_ID": 1,
                            "PRIO": "Error",
                            "ERROR_CODE": "E1",
                            "SUBSYSTEM": "Server",
                            "DATA": "password SECRET",
                        }
                    ] * 2

            def fetchall(self):
                return self.rows

        first = collect_error_table(DB(), source_epoch="e")
        self.assertEqual(len(first["payload"]["events"]), 2)
        self.assertNotEqual(
            first["payload"]["events"][0]["event_id"], first["payload"]["events"][1]["event_id"]
        )
        self.assertNotIn("SECRET", str(first))
        second = collect_error_table(DB(), first["cursor"], source_epoch="e")
        self.assertEqual(second["payload"]["events"], [])

    def test_table_requeries_bounded_window_and_preserves_late_arrival_warning(self):
        class DB:
            def cursor(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, args=None):
                if "@@global.slow_query_log" in sql:
                    self.rows = [{"enabled": 1, "output": "TABLE,FILE"}]
                elif "information_schema.columns" in sql:
                    self.rows = [
                        {"COLUMN_NAME": name} for name in ("start_time", "query_time", "sql_text")
                    ]
                else:
                    self.bound = args[0]
                    self.rows = [
                        {
                            "start_time": "2026-11-08T00:00:00Z",
                            "query_time": 4000,
                            "sql_text": "SELECT 'SECRET'",
                        }
                    ]

            def fetchall(self):
                return self.rows

        db = DB()
        value = collect_slow_table(db, lookback_seconds=999999)
        self.assertEqual(db.bound, 3600)
        self.assertFalse(value["coverage"]["complete"])
        self.assertTrue(value["coverage"]["late_arrival_possible"])
        self.assertNotIn("SECRET", str(value))

    def test_file_append_retains_partial_records_and_redacts_literals(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / "slow.log"
            path.write_bytes(b"")
            sftp = LocalSFTP(root)
            first = read_slow_file(sftp, str(path), [str(path)])
            path.write_bytes(RECORD[:80])
            partial = read_slow_file(sftp, str(path), [str(path)], first["cursor"])
            self.assertEqual(partial["payload"]["events"], [])
            with path.open("ab") as f:
                f.write(RECORD[80:])
            result = read_slow_file(sftp, str(path), [str(path)], partial["cursor"])
            self.assertEqual(len(result["payload"]["events"]), 1)
            self.assertNotIn("PLACEHOLDER_SECRET", str(result))
            self.assertIn("?", result["payload"]["events"][0]["sql_shape"])
            again = read_slow_file(sftp, str(path), [str(path)], result["cursor"])
            self.assertEqual(again["payload"]["events"], [])
            self.assertFalse(result["coverage"]["complete"])
            self.assertEqual(result["coverage"]["identity_confidence"], "fingerprint_only")

    def test_copytruncate_and_path_escape_are_visible(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / "slow.log"
            path.write_bytes(RECORD)
            sftp = LocalSFTP(root)
            first = read_slow_file(sftp, str(path), [str(path)], initial_mode="recent")
            path.write_bytes(b"")
            result = read_slow_file(sftp, str(path), [str(path)], first["cursor"])
            self.assertIn("copytruncate", result["coverage"]["gaps"])
            self.assertNotEqual(first["cursor"]["source_epoch"], result["cursor"]["source_epoch"])
            denied = read_slow_file(sftp, str(Path(root) / "other.log"), [str(path)])
            self.assertEqual(denied["error_code"], "path_not_allowed")

    def test_changed_symlink_cannot_redefine_previously_approved_path(self):
        class Redirected:
            def normalize(self, path):
                return "/outside/secret.log"

        value = read_slow_file(Redirected(), "/approved/slow.log", ["/approved/slow.log"])
        self.assertEqual(value["error_code"], "path_not_allowed")

    def test_redaction_strips_comments_and_all_quoted_payloads(self):
        result = redact_sql(
            "SELECT 'secret', 123, 0xDEADBEEF /* password */ -- secret\nFROM `table` WHERE x=\"double-secret\";"
        )
        self.assertNotIn("secret", result)
        self.assertNotIn("123", result)
        self.assertNotIn("DEADBEEF", result)
        self.assertIn("`table`", result)

    def test_unterminated_literals_and_comments_are_not_exposed(self):
        self.assertNotIn("SECRET", redact_sql("SELECT 'SECRET;"))
        self.assertNotIn("SECRET", redact_sql("SELECT 1 /* SECRET"))

    def test_restart_banner_after_completed_record_does_not_destroy_event(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / "slow.log"
            path.write_bytes(
                RECORD
                + b"/usr/sbin/mysqld, Version: 8.4.5 started with:\nTcp port: 3306\nTime                 Id Command    Argument\n"
                + RECORD
            )
            value = read_slow_file(LocalSFTP(root), str(path), [str(path)], initial_mode="recent")
            self.assertEqual(len(value["payload"]["events"]), 2)
            self.assertIn("server_restart_banner_seen", value["coverage"]["gaps"])

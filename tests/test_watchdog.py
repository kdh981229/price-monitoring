import base64
import hashlib
import json
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.watchdog import build_report, slot_completed, upload_report


class WatchdogTest(unittest.TestCase):
    def test_builds_deterministic_check_identity(self):
        report = build_report(
            date="2026-08-11",
            slot=8,
            completed=False,
            checked_at=datetime(2026, 8, 11, 8, 45, tzinfo=ZoneInfo("Asia/Seoul")),
            workflow_url="https://github.example/run/1",
        )
        self.assertEqual(report["check_id"], "watchdog-20260811-08")
        self.assertEqual(report["outcome"], "missing")
        self.assertEqual(report["collector_grace_minutes"], 28)

    @patch("src.watchdog.post_json")
    def test_slot_status_requires_boolean(self, post_json):
        post_json.return_value = {"ok": True, "completed": True}
        self.assertTrue(slot_completed("https://example.test", "secret", "2026-08-11", 8))

        post_json.return_value = {"ok": True, "completed": "yes"}
        with self.assertRaises(RuntimeError):
            slot_completed("https://example.test", "secret", "2026-08-11", 8)

    @patch("src.watchdog.post_json")
    def test_upload_hash_matches_exact_report_bytes(self, post_json):
        post_json.return_value = {"ok": True}
        report = {
            "check_id": "watchdog-20260811-08",
            "record_type": "watchdog_check",
            "message": "정상",
        }
        upload_report("https://example.test", "secret", report)
        payload = post_json.call_args.args[1]
        decoded = base64.b64decode(payload["report_base64"])
        self.assertEqual(hashlib.sha256(decoded).hexdigest(), payload["report_sha256"])
        self.assertEqual(json.loads(decoded)["message"], "정상")


if __name__ == "__main__":
    unittest.main()

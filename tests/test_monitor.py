import json
import tempfile
import urllib.error
import unittest

from unittest.mock import MagicMock, patch

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.monitor import analysis_input, canonical_url, classify_product, display_price, evaluate, post_drive_gateway, post_to_drive, scheduled_slot_hour


PRODUCTS = [
    {
        "id": "bonjeong",
        "strong_phrases": ["정관장홍삼본정", "홍삼본정"],
        "weak_terms": ["본정"],
        "context_terms": ["정관장", "홍삼"],
        "exclude_terms": ["본정스틱"],
    }
]


class MonitorRulesTest(unittest.TestCase):
    def test_similar_stick_is_excluded(self):
        product, confidence = classify_product("정관장 본정스틱", "홍삼", PRODUCTS)
        self.assertIsNone(product)
        self.assertEqual(confidence, "none")

    def test_short_name_requires_context(self):
        product, confidence = classify_product("본정 선물세트", "정관장 홍삼", PRODUCTS)
        self.assertEqual(product["id"], "bonjeong")
        self.assertEqual(confidence, "medium")

    def test_boga_thresholds(self):
        policy = {"type": "minimum_display_price", "normal_min_krw": 80000, "critical_below_krw": 78000}
        self.assertEqual(evaluate(policy, "https://example.com/1", 77999)[0], "critical")
        self.assertEqual(evaluate(policy, "https://example.com/1", 78000)[0], "warning")
        self.assertEqual(evaluate(policy, "https://example.com/1", 80000)[0], "normal")

    def test_shipping_display(self):
        self.assertEqual(display_price(79000, 3000, "confirmed"), "79,000원 (+3,000원)")
        self.assertEqual(display_price(79000, None, "unknown"), "79,000원 (배송비 확인불가)")

    def test_tracking_parameters_are_removed(self):
        url = canonical_url("https://shop.example/item/1?utm_source=x&item=2", "https://example.com")
        self.assertEqual(url, "https://shop.example/item/1?item=2")

    @patch("src.monitor.time.sleep")
    @patch("src.monitor.urllib.request.urlopen")
    def test_drive_gateway_retries_one_transient_404(self, urlopen, sleep):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true}'
        response.status = 200
        urlopen.side_effect = [
            urllib.error.HTTPError("https://example.test", 404, "Not Found", None, None),
            response,
        ]
        self.assertEqual(post_drive_gateway("https://example.test", {"action": "slot_status"}, 30), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_delayed_08_schedule_keeps_08_slot(self):
        config = {"schedule_hours_kst": [0, 4, 8, 12, 16, 20]}
        delayed_start = datetime(2026, 8, 7, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(scheduled_slot_hour(config, delayed_start, 8), 8)


    def test_analysis_input_preserves_collection_failure_as_unknown(self):
        result = {
            "run": {
                "run_id": "run-20260821T080700+0900",
                "started_at": "2026-08-21T08:07:00+09:00",
                "scheduled_slot_hour_kst": 8,
            },
            "summary": {
                "listing_count": 0, "seller_count": 0, "error_count": 1,
                "rule_counts": {"critical": 0, "review_required": 0, "warning": 0, "normal": 0},
            },
            "source_statuses": {
                "gmarket": {
                    "status": "failed", "collection_method": "public_html",
                    "queries_succeeded": 0, "queries_attempted": 3, "matched_candidates": 0,
                    "incident": {"classification": "limitation"},
                }
            },
            "listings": [], "sellers": [],
            "rules_snapshot": [{
                "id": "sample", "name": "예시 상품",
                "policy": {"type": "minimum_display_price", "normal_min_krw": 80000, "critical_below_krw": 78000},
            }],
            "errors": [{
                "source": "gmarket", "stage": "search_fetch", "category": "http_forbidden",
                "classification": "limitation", "message": "HTTP Error 403",
            }],
            "incidents": [],
        }

        document = analysis_input(result)

        self.assertIn("| gmarket | failed |", document)
        self.assertIn("탐지 결과 없음(채널별 수집 상태 확인 필요)", document)
        self.assertIn("`failed` 또는 `partial` 채널은 위반 없음이나 상품 삭제로 단정하지 않습니다.", document)
        self.assertIn("HTTP Error 403", document)

    @patch("src.monitor.post_drive_gateway")
    def test_drive_payload_includes_daily_analysis_input(self, post_gateway):
        post_gateway.return_value = {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "run-20260821T080700+0900.json"
            result_path.write_text(json.dumps({"run": {"run_id": "run-20260821T080700+0900"}}), encoding="utf-8")
            briefing_path = root / "2026-08-21_08시_기본_브리핑.md"
            briefing_path.write_text("briefing", encoding="utf-8")
            analysis_path = root / "2026-08-21_AI_가격분석_입력.md"
            analysis_path.write_text("analysis", encoding="utf-8")

            post_to_drive("https://example.test", "secret", result_path, briefing_path, analysis_path)

        payload = post_gateway.call_args.args[1]
        self.assertEqual(payload["analysis_input_name"], analysis_path.name)
        self.assertIn("analysis_input_base64", payload)
        self.assertIn("analysis_input_sha256", payload)


if __name__ == "__main__":
    unittest.main()


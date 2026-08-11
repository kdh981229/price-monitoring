import unittest

from datetime import datetime
from zoneinfo import ZoneInfo

from src.monitor import (
    actionable_price, canonical_url, classify_product, display_price, evaluate,
    manual_seller_metadata, naver_shopping_candidates, scheduled_slot_hour, source_error,
    trigger_metadata,
)


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

    def test_delayed_08_schedule_keeps_08_slot(self):
        config = {"schedule_hours_kst": [0, 4, 8, 12, 16, 20]}
        delayed_start = datetime(2026, 8, 7, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(scheduled_slot_hour(config, delayed_start, 8), 8)

    def test_naver_api_price_is_explicit_observation(self):
        payload = {"items": [{
            "title": "정관장 홍삼보가", "link": "https://shop.example/item/1",
            "mallName": "예시몰", "lprice": "79000",
        }]}
        candidate = naver_shopping_candidates(payload, "https://openapi.naver.com", 10)[0]
        self.assertEqual(candidate["price_observation"]["price_krw"], 79000)
        self.assertEqual(candidate["price_observation"]["basis"], "naver_shopping_lprice")
        self.assertTrue(candidate["price_observation"]["actionable"])

    def test_conflicting_price_evidence_is_not_actionable(self):
        observations = [
            {"price_krw": 79000, "actionable": True},
            {"price_krw": 75000, "actionable": True},
        ]
        self.assertIsNone(actionable_price(observations))

    def test_registry_requires_exact_verified_url(self):
        records = [{
            "verification_status": "verified", "match_urls": ["https://shop.example/item/1"],
            "company_name": "확인판매자", "public_phone": "02-1234-5678",
        }]
        self.assertEqual(manual_seller_metadata(records, "https://shop.example/item/1")["company_name"], "확인판매자")
        self.assertEqual(manual_seller_metadata(records, "https://shop.example/item/2"), {})

    def test_gmarket_403_is_method_limitation(self):
        error = source_error({"id": "gmarket"}, "홍삼보가", RuntimeError("HTTP Error 403"), "search_fetch")
        self.assertEqual(error["classification"], "limitation")

    def test_delayed_scheduled_run_creates_scheduler_error(self):
        now = datetime(2026, 8, 11, 3, 2, tzinfo=ZoneInfo("Asia/Seoul"))
        trigger, incidents = trigger_metadata(now, 0, "schedule", 7, "123", "https://example.com/run/123")
        self.assertEqual(trigger["started_late_minutes"], 175)
        self.assertEqual(incidents[0]["classification"], "error")


if __name__ == "__main__":
    unittest.main()

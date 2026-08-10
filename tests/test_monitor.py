import unittest

from datetime import datetime
from zoneinfo import ZoneInfo

from src.monitor import canonical_url, classify_product, display_price, evaluate, scheduled_slot_hour


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


if __name__ == "__main__":
    unittest.main()


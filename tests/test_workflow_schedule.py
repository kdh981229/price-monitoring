import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowScheduleTest(unittest.TestCase):
    def test_each_scheduled_slot_has_its_own_cron_expression(self):
        collect = (ROOT / ".github/workflows/collect.yml").read_text(encoding="utf-8")
        watchdog = (ROOT / ".github/workflows/watchdog.yml").read_text(encoding="utf-8")
        self.assertNotIn("3,7,11,15,19,23", collect)
        self.assertNotIn("3,7,11,15,19,23", watchdog)
        self.assertEqual(set(re.findall(r'cron: "7 (\d+) \* \* \*"', collect)), {"3", "7", "11", "15", "19", "23"})
        self.assertEqual(set(re.findall(r'cron: "17 (\d+) \* \* \*"', collect)), {"3", "7", "11", "15", "19", "23"})
        self.assertEqual(set(re.findall(r'cron: "45 (\d+) \* \* \*"', watchdog)), {"3", "7", "11", "15", "19", "23"})

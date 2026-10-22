"""The Grok briefing is retired: nothing calls xAI, and old stored fields never reach the page.

Synthetic data only.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scraper  # noqa: E402

RETIRED_SOURCES = [
    "scraper.py",
    "glance.py",
    "classroom.py",
    "index.html",
    ".github/workflows/scrape.yml",
    ".github/workflows/scrape-after-school.yml",
]


class NoXaiCallerTests(unittest.TestCase):
    def test_no_xai_endpoint_key_or_grok_only_flag(self):
        for rel in RETIRED_SOURCES:
            src = (ROOT / rel).read_text()
            for needle in ("api.x.ai", "GROK_API_KEY", "grok_only", "--grok-only", "grok-4"):
                self.assertNotIn(needle, src, f"{needle} still in {rel}")

    def test_briefing_functions_are_gone(self):
        for name in ("generate_ai_summary", "regenerate_grok_summaries", "generate_look_next",
                     "attach_look_next_grok", "sanitize_briefing_copy", "log_grok_usage"):
            self.assertFalse(hasattr(scraper, name), name)

    def test_page_has_no_briefing_block(self):
        html = (ROOT / "index.html").read_text()
        for needle in ("masthead", "<h2>Briefing</h2>", "No briefing yet", "function findAiClass(", "panel-story"):
            self.assertNotIn(needle, html)


class RetiredFieldTests(unittest.TestCase):
    def test_drop_retired_grok_fields(self):
        student = {
            "sn": "9001",
            "ai_summary": {"headline": "Synthetic headline", "classes": [{"class_name": "Art", "story": "x"}]},
            "look_next_grok": "Synthetic paragraph",
            "view": {
                "headline": "Synthetic headline",
                "wins": ["Synthetic win"],
                "generated_at": "2026-10-01T08:00:00Z",
                "tonight_label": "Art: Sketch",
                "classes": [{"course_name": "Art", "snap": "On track", "story": "Synthetic story"}],
                "glance": {"look_next": ["Art has 1 past due."], "look_next_grok": "Synthetic"},
            },
        }
        scraper.drop_retired_grok_fields(student)
        self.assertNotIn("ai_summary", student)
        self.assertNotIn("look_next_grok", student)
        view = student["view"]
        for key in ("headline", "wins", "generated_at"):
            self.assertNotIn(key, view)
        self.assertEqual(view["tonight_label"], "Art: Sketch")
        self.assertEqual(view["classes"], [{"course_name": "Art", "snap": "On track"}])
        self.assertEqual(view["glance"], {"look_next": ["Art has 1 past due."]})

    def test_drop_is_safe_on_odd_input(self):
        self.assertIsNone(scraper.drop_retired_grok_fields(None))
        self.assertEqual(scraper.drop_retired_grok_fields({"view": None}), {"view": None})


if __name__ == "__main__":
    unittest.main()

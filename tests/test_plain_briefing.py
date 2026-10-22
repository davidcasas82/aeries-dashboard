import io
import json
import re
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scraper  # noqa: E402
from test_change_snapshot import TODAY, _student, _yesterday  # noqa: E402

EVENING = datetime(2026, 9, 25, 20, 7, tzinfo=scraper.PACIFIC)


class SinceYesterdayTests(unittest.TestCase):
    def test_evening_note_is_the_code_phrase_with_no_grok_call(self):
        history = {"students": {"1": {"name": "", "snapshots": [_yesterday()]}}}
        with mock.patch.object(scraper, "GROK_API_KEY", "test-key"), \
                mock.patch.object(scraper.requests, "post") as post, \
                mock.patch.object(scraper, "pacific_today", return_value=TODAY.date()), \
                mock.patch.object(scraper, "pacific_today_dt", return_value=TODAY):
            snap = scraper.apply_daily_change_snapshot(history, "1", "", _student(), [], now=EVENING)
        post.assert_not_called()
        self.assertTrue(snap["since_yesterday"])
        self.assertEqual(snap["since_yesterday"], scraper.phrase_since_yesterday(snap["change_events"]))


class TonightPlanTests(unittest.TestCase):
    def test_plan_overwrites_whatever_the_model_wrote(self):
        summary = {
            "focus_tonight": "Redo the Unit 1 quiz",
            "classes": [
                {"class_name": "Algebra", "do_tonight": "Redo the Unit 1 quiz"},
                {"class_name": "Biology", "do_tonight": "Read chapter 2"},
            ],
        }
        analytics = {"tonight_plan": {
            "label": "Algebra: Worksheet",
            "items": [{"class_name": "Algebra", "name": "Worksheet"}],
        }}
        out = scraper.apply_tonight_plan(summary, analytics)
        self.assertEqual(out["focus_tonight"], "Algebra: Worksheet")
        self.assertEqual([c["do_tonight"] for c in out["classes"]], ["Worksheet", ""])


class GrokUsageTests(unittest.TestCase):
    def test_usage_line(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            scraper.log_grok_usage("briefing", {"usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}})
            scraper.log_grok_usage("look next", {})
        self.assertEqual(buf.getvalue().splitlines(), [
            "  Grok usage (briefing): prompt=1200 completion=300 total=1500",
            "  Grok usage (look next): prompt=? completion=? total=?",
        ])

    def test_a_malformed_usage_never_raises(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            for result in (None, [], "text", {"usage": None}, {"usage": "12 tokens"}, {"usage": [1, 2]}, {"usage": 7}):
                scraper.log_grok_usage("briefing", result)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 7)
        self.assertEqual(set(lines), {"  Grok usage (briefing): prompt=? completion=? total=?"})

    def test_briefing_call_logs_its_usage(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"headline": "x", "classes": []})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
        buf = io.StringIO()
        with mock.patch.object(scraper, "GROK_API_KEY", "test-key"), \
                mock.patch.object(scraper.requests, "post", return_value=response), \
                redirect_stdout(buf):
            scraper.generate_ai_summary({
                "name": "Fixture",
                "classes": [{"period": 1, "course_name": "Algebra", "percent": "88", "mark": "B"}],
                "assignments_by_class": [{"class_name": "1 Algebra", "period": 1, "assignments": [
                    {"description": "Quiz", "points_earned": 8, "points_possible": 10, "due_date": "09/20/2026"},
                ]}],
            })
        self.assertIn("  Grok usage (briefing): prompt=10 completion=2 total=12", buf.getvalue().splitlines())


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UrgencyColorTests(unittest.TestCase):
    """Row colors come from the scraper's suggested_urgency, never the model's urgency."""

    def test_suggested_urgency_drives_the_row(self):
        html = Path(__file__).resolve().parents[1].joinpath("index.html").read_text()
        fn = re.search(r"^( *)function inferUrgency\(.*?^\1\}$", html, re.M | re.S).group(0)
        script = (
            "function hasRealGrade() { return true; }\n"
            "function isDueTodayUngraded() { return false; }\n"
            f"{fn}\n"
            "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
            "process.stdout.write(JSON.stringify(cases.map(c => inferUrgency(c, c.ai))));\n"
        )
        base = {"classMeta": {"percent": "85", "mark": "B"}, "missingCount": 0, "upcoming": []}
        cases = [
            {**base, "suggestedUrgency": "ok", "ai": {"urgency": "strong"}},
            {**base, "ai": {"urgency": "strong"}},
            {**base, "suggestedUrgency": "watch", "missingCount": 1},
            {**base, "classMeta": {"percent": "95", "mark": "A"}, "suggestedUrgency": "watch"},
        ]
        done = subprocess.run(["node", "-e", script], input=json.dumps(cases), capture_output=True,
                              text=True, check=True)
        self.assertEqual(json.loads(done.stdout), ["ok", "ok", "watch", "strong"])


if __name__ == "__main__":
    unittest.main()

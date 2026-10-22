import json
import re
import shutil
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scraper  # noqa: E402
from test_change_snapshot import TODAY, _student, _yesterday  # noqa: E402

EVENING = datetime(2026, 9, 25, 20, 7, tzinfo=scraper.PACIFIC)


class SinceYesterdayTests(unittest.TestCase):
    def test_evening_note_is_the_code_phrase_with_no_model_call(self):
        history = {"students": {"1": {"name": "", "snapshots": [_yesterday()]}}}
        with mock.patch.object(scraper.requests, "post") as post, \
                mock.patch.object(scraper, "pacific_today", return_value=TODAY.date()), \
                mock.patch.object(scraper, "pacific_today_dt", return_value=TODAY):
            snap = scraper.apply_daily_change_snapshot(history, "1", "", _student(), [], now=EVENING)
        post.assert_not_called()
        self.assertTrue(snap["since_yesterday"])
        self.assertEqual(snap["since_yesterday"], scraper.phrase_since_yesterday(snap["change_events"]))


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

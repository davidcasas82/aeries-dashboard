"""The page's last-updated line and stale banner use the data's own timestamp."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = Path(__file__).resolve().parents[1].joinpath("index.html").read_text()


def _fn(name):
    m = re.search(rf"^( *)function {name}\(.*?^\1\}}$", HTML, re.M | re.S)
    assert m, name
    return m.group(0)


def _view(data, now_iso):
    consts = "\n".join(re.findall(r"^ *const (?:STALE_HOURS|VERY_STALE_HOURS) = .*$", HTML, re.M))
    script = (
        f"{consts}\n{_fn('lastUpdatedView')}\n"
        "const c = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        "process.stdout.write(JSON.stringify(lastUpdatedView(c.data, new Date(c.now))));\n"
    )
    out = subprocess.run(["node", "-e", script], input=json.dumps({"data": data, "now": now_iso}),
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FreshnessTests(unittest.TestCase):
    def test_banner_markup_present(self):
        self.assertIn('id="staleBanner"', HTML)

    def test_fresh_shows_pt_time_from_data_and_no_banner(self):
        v = _view({"last_updated": "2026-10-13T22:30:00Z"}, "2026-10-14T01:00:00Z")
        self.assertEqual(v["text"], "Last updated today 3:30 PM PT")
        self.assertEqual(v["banner"], "")
        self.assertEqual(v["cls"], "last-updated")

    def test_stale_past_threshold_shows_banner(self):
        v = _view({"last_updated": "2026-10-13T00:00:00Z"}, "2026-10-14T00:00:00Z")
        self.assertIn("stale", v["cls"])
        self.assertIn("no new data for 24 hours", v["banner"])
        self.assertIn("PT", v["banner"])

    def test_very_stale(self):
        v = _view({"last_updated": "2026-10-11T00:00:00Z"}, "2026-10-14T00:00:00Z")
        self.assertTrue(v["text"].startswith("Not updated since Oct 10"))
        self.assertIn("very-stale", v["bannerCls"])

    def test_summer_break_never_stale(self):
        v = _view({"last_updated": "2026-07-01T00:00:00Z", "summer_break": True}, "2026-07-20T00:00:00Z")
        self.assertEqual(v["banner"], "")

    def test_missing_timestamp_is_blank(self):
        self.assertEqual(_view({}, "2026-10-14T00:00:00Z")["text"], "")


if __name__ == "__main__":
    unittest.main()

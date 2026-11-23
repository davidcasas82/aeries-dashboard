"""Class rows open the detail drawer, and that drawer links to class history."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.parse import quote


HTML = Path(__file__).resolve().parents[1].joinpath("index.html").read_text()


def _fn(name):
    match = re.search(rf"^( *)function {name}\(.*?^\1\}}$", HTML, re.M | re.S)
    if not match:
        raise AssertionError(name)
    return match.group(0)


class StandingDrawerSmokeTests(unittest.TestCase):
    def test_class_rows_carry_data_glance(self):
        button = re.search(r'<button class="glance-grade".*?</button>', HTML, re.S)
        self.assertIsNotNone(button)
        markup = button.group(0)
        self.assertIn('data-glance="${esc(card.id)}"', markup)
        self.assertNotIn("data-progress", markup)
        self.assertIn("Open detail for", markup)
        self.assertIn("Tap a class for its assignments and history.", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_drawer_renders_a_history_link(self):
        drawer = re.search(r'<aside class="glance-drawer".*?</aside>', HTML, re.S)
        self.assertIsNotNone(drawer)
        anchor = re.search(
            r'<a class="glance-history" id="glanceHistory"[^>]*>([^<]+)</a>',
            drawer.group(0),
        )
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.group(1), "History")
        self.assertIn("historyLink.href = standingHistoryHref(student, course);", HTML)
        script = (
            _fn("standingHistoryHref")
            + "\nprocess.stdout.write(standingHistoryHref({ sn: '9' }, 'Lab & Lecture'));\n"
        )
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            done.stdout,
            "progress.html?student=9#" + quote("Lab & Lecture", safe=""),
        )


class ProgressChipTests(unittest.TestCase):
    def test_header_control_stays_on_the_progress_page(self):
        self.assertIn(
            'id="progressLink" href="progress.html">Progress &amp; history',
            HTML,
        )
        self.assertIn('class="progress-link-chevron"', HTML)

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


def _css_block(html, header):
    start = html.find(header)
    if start < 0:
        raise AssertionError(header)
    brace = html.find("{", start)
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[brace + 1:i]
    raise AssertionError(f"unclosed {header}")


class MobileDrawerSheetTests(unittest.TestCase):
    def test_phone_sheet_is_one_tall_scroll_and_desktop_still_pins_the_header(self):
        mobile = _css_block(HTML, "@media (max-width: 620px)")
        drawer = _css_block(mobile, ".glance-drawer {")
        self.assertIn("top: calc(env(safe-area-inset-top, 0px) + 12px);", drawer)
        self.assertIn("max-height: calc(100dvh - env(safe-area-inset-top, 0px) - 12px);", drawer)
        self.assertIn("overflow-y: auto;", drawer)
        self.assertNotIn("78vh", mobile)
        self.assertIn("display: contents;", _css_block(mobile, ".glance-drawer-top {"))
        self.assertIn("overflow: visible;", _css_block(mobile, ".glance-drawer-body {"))
        close = _css_block(mobile, ".glance-close {")
        self.assertIn("position: sticky;", close)
        self.assertIn("top: 0;", close)
        self.assertIn("grid-row: 1 / span 2;", close)
        self.assertNotIn("1 / -1", close)
        desktop_body = HTML.split("@media (max-width: 620px)")[0]
        self.assertIn(".glance-drawer-body { overflow: auto; padding: 28px 2px 8px; }", desktop_body)
        self.assertIn("flex-shrink: 0;", _css_block(desktop_body, ".glance-drawer-top {"))
        self.assertNotIn("position: sticky;", _css_block(desktop_body, ".glance-close {"))


class ProgressChipTests(unittest.TestCase):
    def test_header_control_stays_on_the_progress_page(self):
        self.assertIn(
            'id="progressLink" href="progress.html">Progress &amp; history',
            HTML,
        )
        self.assertIn('class="progress-link-chevron"', HTML)

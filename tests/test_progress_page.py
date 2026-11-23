"""One kid's progress markup never includes the other kid's classes."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


HTML = Path(__file__).resolve().parents[1].joinpath("progress.html").read_text()
FUNCS = [
    "esc", "spark", "grade", "impacts", "moveBlock", "badges",
    "renderKid", "renderClass", "selectedStudent", "progressMarkup", "tagsFor",
]


def _js_functions(names):
    out = []
    for name in names:
        match = re.search(rf"^( *)function {name}\(.*?^\1\}}$", HTML, re.M | re.S)
        if not match:
            raise AssertionError(name)
        out.append(match.group(0))
    return "\n".join(out)


def _kid(name, sn, course, line):
    return {
        "name": name,
        "sn": sn,
        "summary": f"{name} summary",
        "classes": [{
            "course": course,
            "letter": "A",
            "percent": 90,
            "note": "",
            "series": [{"date": "2026-09-11", "pct": 90}, {"date": "2026-11-22", "pct": 91}],
            "recent": [],
            "earlier": [],
            "weak_spots": [],
            "was_now": {"text": line, "direction": "up"},
        }],
    }


def render(selected, course):
    doc = {"students": [_kid("Ada", "a", "Algebra", "Ada line"), _kid("Bea", "b", "Biology", "Bea line")]}
    script = (
        _js_functions(FUNCS)
        + "\nconst data = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        + "process.stdout.write(progressMarkup(data.doc, {}, data.selected, data.course));\n"
    )
    done = subprocess.run(
        ["node", "-e", script],
        input=json.dumps({"doc": doc, "selected": selected, "course": course}),
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ProgressPageKidTests(unittest.TestCase):
    def test_the_page_does_not_render_every_kid(self):
        self.assertNotIn("doc.students.map", HTML)
        self.assertIn("progressMarkup(", HTML)

    def test_one_kids_view_excludes_the_other_kids_classes(self):
        ada = render("a", "")
        bea = render("b", "")
        self.assertIn("Algebra", ada)
        self.assertIn("Ada", ada)
        self.assertNotIn("Biology", ada)
        self.assertNotIn("Bea", ada)
        self.assertIn("Biology", bea)
        self.assertIn("Bea", bea)
        self.assertNotIn("Algebra", bea)
        self.assertNotIn("Ada", bea)

        ada_history = render("a", "Biology")
        bea_history = render("b", "Algebra")
        self.assertNotIn("Bea line", ada_history)
        self.assertNotIn("Biology", ada_history)
        self.assertNotIn("Ada line", bea_history)
        self.assertNotIn("Algebra", bea_history)

        ada_own = render("a", "Algebra")
        self.assertIn("Ada line", ada_own)
        self.assertNotIn("Bea line", ada_own)
        self.assertNotIn("Biology", ada_own)

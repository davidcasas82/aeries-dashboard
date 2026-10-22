"""The dashboard's local grade notes (PR5): plain-text rendering, class matching, and Grok takeover.

Runs the page's own functions under node with a tiny fake DOM whose innerHTML setter throws, so a
title or story can only ever land as text. Synthetic data only.
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import insights_writer  # noqa: E402

HTML = Path(__file__).resolve().parents[1].joinpath("index.html").read_text()
FUNCS = ["normalizeName", "localWriteUpFor", "matchLocalStories", "withLocalWriteUp", "shortPostedDate", "textEl",
         "buildLocalInsights"]

FAKE_DOM = r"""
const innerHtmlWrites = [];
function fakeEl(tag) {
    const el = {
        tagName: tag.toUpperCase(), className: '', attrs: {}, childNodes: [], _text: '',
        appendChild(child) { this.childNodes.push(child); return child; },
        setAttribute(k, v) { this.attrs[k] = String(v); },
        set textContent(v) { this._text = String(v); this.childNodes = []; },
        get textContent() { return this._text + this.childNodes.map(c => c.textContent).join(''); },
    };
    Object.defineProperty(el, 'innerHTML', {
        set(v) { innerHtmlWrites.push(String(v)); throw new Error('innerHTML used'); },
        get() { throw new Error('innerHTML read'); },
    });
    return el;
}
const document = { createElement: fakeEl };
function walk(el, out = []) { out.push(el); el.childNodes.forEach(c => walk(c, out)); return out; }
"""


def _js_functions(names):
    out = []
    for name in names:
        m = re.search(rf"^( *)function {name}\(.*?^\1\}}$", HTML, re.M | re.S)
        assert m, name
        out.append(m.group(0))
    return "\n".join(out)


def run_page(body, data):
    script = (
        f"let localInsights = null;\n{FAKE_DOM}\n{_js_functions(FUNCS)}\n"
        "const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        f"{body}\n"
    )
    done = subprocess.run(["node", "-e", script], input=json.dumps(data), capture_output=True, text=True)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


SCRIPT_TITLE = "<script>alert(1)</script>"
IMG_TITLE = "<img src=x onerror=alert(1)>"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LocalNotesPageTests(unittest.TestCase):
    def test_malicious_class_and_assignment_titles_render_as_plain_text(self):
        up = {
            "as_of": "2026-10-21",
            "stories": [
                {"class": SCRIPT_TITLE, "story": f"Still a B. The {IMG_TITLE} (4/10) hurt.", "tags": ["biggest_risk"]},
                {"class": "Biology", "story": f"Still an A. The {SCRIPT_TITLE} Lab (9/10) helped."},
            ],
        }
        got = run_page(
            "const { section } = buildLocalInsights(data.up, data.courses);\n"
            "const nodes = walk(section);\n"
            "process.stdout.write(JSON.stringify({\n"
            "  writes: innerHtmlWrites,\n"
            "  tags: nodes.map(n => n.tagName),\n"
            "  texts: nodes.filter(n => !n.childNodes.length).map(n => n._text),\n"
            "}));",
            {"up": up, "courses": [SCRIPT_TITLE, "Biology", "Art"]},
        )
        self.assertEqual(got["writes"], [])
        self.assertNotIn("SCRIPT", got["tags"])
        self.assertNotIn("IMG", got["tags"])
        self.assertIn(SCRIPT_TITLE, got["texts"])
        self.assertIn("Biggest risk", got["texts"])
        self.assertIn(up["stories"][0]["story"], got["texts"])
        self.assertIn(up["stories"][1]["story"], got["texts"])

    def test_the_notes_code_never_touches_innerHTML(self):
        start = HTML.index("// ── Local grade notes")
        end = HTML.index("function glanceItemById(")
        self.assertNotIn("innerHTML", HTML[start:end])
        self.assertNotIn("insertAdjacentHTML", HTML[start:end])
        self.assertIn("localStoryEl.textContent = item.local_story", HTML)

    def test_page_matching_agrees_with_the_writer(self):
        cases = [
            ([("Math", "a"), ("AP Math Analysis", "b")], ["AP Math Analysis", "math"]),
            ([("AP Math Analysis", "a"), ("Math Honors", "b")], ["Math", "Biology"]),
            ([("Math", "a")], ["Math", "Math Lab"]),
            ([("Biology", "a")], ["P3 Biology"]),
            ([("Biology", "a")], ["P3 Biology", "P5 Biology Lab"]),
            ([("Biology", "a")], ["Biology Lab", "Biology"]),
            ([("Art", "a")], ["Art", "ART"]),
            ([("Int Eng&Marktg", "a"), ("Physics & Eng", "b")], ["Int Eng & Marktg", "Physics & Eng", "Eng"]),
            ([], ["Math"]),
            ([("Math", "a")], ["", "Math"]),
            ([("", "a"), ("Math", "b")], ["Math", ""]),
            ([(None, "a"), ("Art", "b")], ["Art", "Math"]),
            ([("!!!", "a"), ("Art", "b")], ["&&", "Art", "..."]),
            ([("Math", "a")], [None, "Math"]),
        ]
        data = [{"stories": [{"class": c, "story": t} for c, t in stories], "names": names}
                for stories, names in cases]
        page = run_page(
            "process.stdout.write(JSON.stringify(data.map(c => "
            "matchLocalStories(c.stories, c.names).map(p => p && p.story))));",
            data,
        )
        for case, got in zip(data, page):
            want = [p and p["story"] for p in insights_writer.match_stories(case["stories"], case["names"])]
            self.assertEqual(got, want, case)
        self.assertEqual(page[4], [None, None])  # one contained story never lands on two classes

    def test_once_a_local_write_up_exists_no_class_shows_grok_text(self):
        grok = {"class_name": "Art", "story": "Grok story", "status": "Grok status",
                "going_well": "Grok well", "going_wrong": "Grok wrong", "snap": "keep me"}
        kid_a = {"sn": "9001", "ai_summary": {"headline": "Grok briefing", "classes": [grok]},
                 "view": {"classes": [{"course_name": "Art", "story": "Grok view story"}]}}
        kid_b = {"sn": "9002", "ai_summary": {"classes": [grok]}}
        insights = {"students": {"9001": {"stories": []}}}
        got = run_page(
            "process.stdout.write(JSON.stringify([withLocalWriteUp(data.a, data.i), withLocalWriteUp(data.b, data.i),"
            " withLocalWriteUp(data.a, null)]));",
            {"a": kid_a, "b": kid_b, "i": insights},
        )
        a, b, none = got
        cls = a["ai_summary"]["classes"][0]
        self.assertEqual([cls[k] for k in ("story", "status", "going_well", "going_wrong")], ["", "", "", ""])
        self.assertEqual(cls["snap"], "keep me")
        self.assertEqual(a["view"]["classes"][0]["story"], "")
        self.assertEqual(a["ai_summary"]["headline"], "Grok briefing")  # the Grok briefing stays for now
        self.assertEqual(b["ai_summary"]["classes"][0]["story"], "Grok story")  # no write-up for Kid B
        self.assertEqual(none["ai_summary"]["classes"][0]["status"], "Grok status")

    def test_the_posted_through_date_is_short_month_and_day(self):
        got = run_page(
            "process.stdout.write(JSON.stringify({dates: data.dates.map(shortPostedDate), sub: data.ups.map(up =>"
            " walk(buildLocalInsights(up, []).section).find(n => n.tagName === 'P')._text)}));",
            {"dates": ["2026-10-21", "2026-01-05", "2026-13-01", "Oct 21", "", None, "2026-02-31", "2026-04-31",
                       "2028-02-29", "2026-02-29", "2026-00-10", "2026-10-00"],
             "ups": [{"as_of": "2026-10-21", "stories": []}, {"stories": []}]},
        )
        self.assertEqual(got["dates"], ["Oct 21", "Jan 5", "", "", "", "", "", "", "Feb 29", "", "", ""])
        self.assertEqual(got["sub"], ["From grades posted through Oct 21", "From the posted grades"])

    def test_notes_and_drawer_story_text_wrap(self):
        css = HTML[HTML.index(".glance-insight-class,\n"):]
        css = css[:css.index("}") + 1]
        for rule in ("white-space: normal", "overflow-wrap: anywhere", ".glance-local-story", ".glance-insight-story"):
            self.assertIn(rule, css)
        self.assertRegex(HTML, r"\.glance-insight-row,\s*\n\s*\.glance-drawer-top > div \{ min-width: 0; \}")

    def rows(self, up, courses):
        got = run_page(
            "const rows = walk(buildLocalInsights(data.up, data.courses).section)"
            "  .filter(n => n.className === 'glance-insight-row')"
            "  .map(r => r.childNodes.map(n => n.childNodes.length ? n.childNodes.map(c => [c.className, c._text])"
            "    : [n.className, n._text]));\n"
            "process.stdout.write(JSON.stringify({writes: innerHtmlWrites, rows}));",
            {"up": up, "courses": courses},
        )
        self.assertEqual(got["writes"], [])
        return got["rows"]

    def test_tagged_classes_come_first_with_the_tag_after_the_class_name(self):
        story = lambda cls, tags=None: {"class": cls, "story": f"{cls} note.", **({"tags": tags} if tags else {})}
        courses = ["Spanish", "Algebra", "Physics", "Marketing", "Art"]
        win, risk = ["glance-insight-tag win", "Easiest win"], ["glance-insight-tag risk", "Biggest risk"]
        rows = self.rows({"stories": [story("Spanish"), story("Algebra", ["easiest_win"]), story("Physics"),
                                      story("Marketing", ["biggest_risk"]), story("Art")]}, courses)
        self.assertEqual(rows, [
            [[["glance-insight-class", "Algebra"], win], ["glance-insight-story", "Algebra note."]],
            [[["glance-insight-class", "Marketing"], risk], ["glance-insight-story", "Marketing note."]],
            [[["glance-insight-class", "Spanish"]], ["glance-insight-story", "Spanish note."]],
            [[["glance-insight-class", "Physics"]], ["glance-insight-story", "Physics note."]],
            [[["glance-insight-class", "Art"]], ["glance-insight-story", "Art note."]],
        ])
        # Risk listed before win in the payload still shows win first; one class with both shows both, in order.
        both = self.rows({"stories": [story("Spanish"), story("Art", ["biggest_risk", "easiest_win"])]}, ["Spanish", "Art"])
        self.assertEqual(both[0][0], [["glance-insight-class", "Art"], win, risk])
        flipped = self.rows({"stories": [story("Spanish", ["biggest_risk"]), story("Art", ["easiest_win"])]},
                            ["Spanish", "Art"])
        self.assertEqual([r[0][0][1] for r in flipped], ["Art", "Spanish"])
        # No tags: the current order, no tag and no separate top line.
        plain = self.rows({"stories": [story("Spanish"), story("Art")]}, ["Spanish", "Art"])
        self.assertEqual([r[0] for r in plain], [[["glance-insight-class", "Spanish"]], [["glance-insight-class", "Art"]]])
        self.assertNotIn("Top focus area", HTML)
        self.assertNotIn("glance-insights-headline", HTML)

    def test_tags_render_only_their_fixed_labels_as_text(self):
        rows = self.rows({"stories": [{"class": SCRIPT_TITLE, "story": IMG_TITLE,
                                       "tags": [SCRIPT_TITLE, "easiest_win", IMG_TITLE, "<b>biggest_risk</b>"]},
                                      {"class": "Art", "story": "Fine.", "tags": "easiest_win"}]}, [SCRIPT_TITLE, "Art"])
        self.assertEqual(rows, [
            [[["glance-insight-class", SCRIPT_TITLE], ["glance-insight-tag win", "Easiest win"]],
             ["glance-insight-story", IMG_TITLE]],
            [[["glance-insight-class", "Art"]], ["glance-insight-story", "Fine."]],
        ])

    def test_the_tag_is_small_and_tinted_with_theme_colors(self):
        def rule(selector):
            css = HTML[HTML.index(selector + " {"):]
            return css[:css.index("}") + 1]
        tag = rule(".glance-insight-tag")
        for prop in ("font-size: 12px", "border-radius: 999px", "white-space: nowrap"):
            self.assertIn(prop, tag)
        self.assertIn("var(--g-forest-soft)", rule(".glance-insight-tag.win"))
        self.assertIn("var(--g-amber-soft)", rule(".glance-insight-tag.risk"))
        self.assertIn("flex-wrap: wrap", rule(".glance-insight-head"))

    def test_a_class_without_a_local_story_gets_no_row(self):
        up = {"stories": [{"class": "Biology", "story": "Still an A."}]}
        got = run_page(
            "const { section, picks } = buildLocalInsights(data.up, data.courses);\n"
            "process.stdout.write(JSON.stringify({rows: walk(section).filter(n => n.className === "
            "'glance-insight-class').map(n => n._text), picks: picks.map(p => p && p.story)}));",
            {"up": up, "courses": ["Biology", "Art"]},
        )
        self.assertEqual(got["rows"], ["Biology"])
        self.assertEqual(got["picks"], ["Still an A.", None])


if __name__ == "__main__":
    unittest.main()

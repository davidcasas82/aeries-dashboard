"""The runner's publish payload against family-data's real cleanInsights.

tests/fixtures/family_data_insights.js is a verbatim copy of family-data src/insights.js (PR #27 head
d637245, branch cursor/grade-insights-write-path). The sha256 pin below fails if the copy is edited;
when family-data changes that file, copy it again and update the pin. node loads the copy as an ES
module with only its two import lines swapped for stubs (cleanInsights uses neither), so the check runs
the Worker's own code, not a hand port. No network, no cross-repo runtime dependency.
"""
import hashlib
import json
import random
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import insights_runner as runner  # noqa: E402
import insights_writer  # noqa: E402

WORKER = Path(__file__).resolve().parent / "fixtures" / "family_data_insights.js"
WORKER_SHA256 = "127cd18e2377aaefb700d2ed1ae9d2b767bb8ed59a70db5dbcc8c9b078b01108"

NODE_CHECK = r"""
const fs = require('fs');
const assert = require('assert');
(async () => {
  const src = fs.readFileSync(process.argv[1], 'utf8');
  const imports = src.match(/^import .*$/mg) || [];
  if (imports.length !== 2) throw new Error('expected 2 import lines, got ' + imports.length);
  const stubs = 'const timingSafeEqual = () => false;\nconst readBody = async () => ({});\n';
  const code = stubs + src.replace(/^import .*$/mg, '');
  const mod = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
  const bodies = JSON.parse(fs.readFileSync(0, 'utf8'));
  const failures = [];
  bodies.forEach((body, i) => {
    const doc = JSON.parse(body);
    const out = mod.cleanInsights(doc);
    if (out.error) { failures.push([i, out.error]); return; }
    try { assert.deepStrictEqual(out.students, doc.students); } catch (e) { failures.push([i, 'changed']); }
  });
  process.stdout.write(JSON.stringify({ checked: bodies.length, failures }));
})().catch(e => { console.error(e); process.exit(1); });
"""

WS = ["\ufeff", "\u00a0", "\u2028", "\u2029", "\u1680", "\u2000", "\u200a", "\u202f", "\u205f", "\u3000", "\t", "\v"]


def text(rng, lo, hi):
    n = rng.randint(lo, hi)
    pick = rng.random()
    if pick < 0.08:
        return "".join(rng.choice(WS) for _ in range(max(n, 1) % 6 + 1))
    if pick < 0.12:
        return "ok \ud83d"
    if pick < 0.16:
        return "\U0001F600" * (n // 2) + "x"
    if pick < 0.18:
        return "\x85\x1c ok"  # Python strip() whitespace that JS trim keeps
    return ("é" if rng.random() < 0.3 else "x") * n


TAG_CHOICES = [["easiest_win"], ["biggest_risk"], ["easiest_win", "biggest_risk"], ["biggest_risk", "easiest_win"], [],
               None, "easiest_win", ["easiest_win", "easiest_win"], ["Easiest win"], [7], [None], {"easiest_win": 1}]


def random_ups(rng):
    keys = ["9001", "9002", "ab_c", "k" * 32, "k" * 33, "٩٠٠١", 9003, True, None, "a-b", " 9004 ", "9001"]
    ups = []
    for _ in range(rng.randint(0, 11)):
        up = {"sn": rng.choice(keys) if rng.random() < 0.6 else str(rng.randint(1, 99999))}
        if rng.random() < 0.9:
            up["as_of"] = rng.choice(["2026-10-22", "", "\ufeff", "x" * 41, None, 5, "2026-10-22T00:00"])
        if rng.random() < 0.3:
            # An old write-up's headline: the runner never sends it.
            up["headline"] = {"text": text(rng, 1, 620), "cites": ["a"], "class": text(rng, 0, 125)}
        stories = []
        for _ in range(rng.randint(0, 45)):
            n = rng.randint(0, 15)
            cites = [rng.choice(["a", "b", "c", 7, None, text(rng, 1, 125)]) for _ in range(n)]
            story = {"class": text(rng, 0, 125), "story": text(rng, 1, 620), "cites": cites,
                     "slotted": "x", "dropped": ["vote"]}
            if rng.random() < 0.5:
                story["tags"] = rng.choice(TAG_CHOICES)
            stories.append(story)
        up["stories"] = stories if rng.random() < 0.95 else "not a list"
        ups.append(up)
    return ups


def full_ups():
    """8 students x 40 stories at the line maximums: 600-unit text, 12 cites of 120, 120-unit class, both tags."""
    def story(i, j):
        return {"class": f"{i}{j}".ljust(120, "c"), "story": f"{i}-{j} ".ljust(600, "s"),
                "cites": [f"{i}-{j}-{k}".ljust(120, "f") for k in range(12)], "tags": ["easiest_win", "biggest_risk"]}
    return [{"sn": f"90{i:02d}", "as_of": "2026-10-22", "stories": [story(i, j) for j in range(40)]} for i in range(8)]


def run_worker(bodies):
    done = subprocess.run(["node", "-e", NODE_CHECK, str(WORKER)], input=json.dumps(bodies), capture_output=True,
                          text=True, timeout=120)
    if done.returncode:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout)


def sendable(ups):
    """The body the publisher would send, or None when it sends nothing."""
    doc = runner.insights_doc(ups)
    return runner.insights_body(doc).decode("utf-8") if doc["students"] else None


class PinnedWorkerTests(unittest.TestCase):
    def test_the_worker_copy_is_unchanged(self):
        self.assertEqual(hashlib.sha256(WORKER.read_bytes()).hexdigest(), WORKER_SHA256)
        source = WORKER.read_text()
        self.assertIn("const MAX_BYTES = 256 * 1024;", source)
        for name, value in (("MAX_STUDENTS", 8), ("MAX_STORIES", 40), ("MAX_TEXT", 600), ("MAX_CITES", 12)):
            self.assertIn(f"const {name} = {value};", source)
            self.assertEqual(getattr(runner, f"PUBLISH_{name}"), value)
        self.assertEqual(runner.PUBLISH_MAX_BYTES, 256 * 1024)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class RealWorkerTests(unittest.TestCase):
    def test_every_payload_the_runner_sends_passes_the_real_cleanInsights_unchanged(self):
        rng = random.Random(57)
        cases = [full_ups(), full_ups()[:3]] + [random_ups(rng) for _ in range(300)]
        bodies = [b for b in (sendable(ups) for ups in cases) if b is not None]
        self.assertGreater(len(bodies), 150)
        for body in bodies:
            self.assertLessEqual(len(body.encode("utf-8")), runner.PUBLISH_MAX_BYTES)
        result = run_worker(bodies)
        self.assertEqual(result, {"checked": len(bodies), "failures": []})

    def test_the_worker_would_reject_what_the_runner_drops(self):
        # Controls: each raw shape fails the real cleanInsights, so the runner's drops are needed.
        raw = [
            {"students": {"ab_c": {"stories": []}}},
            {"students": {"٩٠٠١": {"stories": []}}},
            {"students": {"9001": {"as_of": "\ufeff", "stories": []}}},
            {"students": {"9001": {"stories": [{"class": "\u3000", "story": "x", "cites": ["a"]}]}}},
            {"students": {"9001": {"stories": [{"class": "B", "story": "x" * 601, "cites": ["a"]}]}}},
            {"students": {"9001": {"stories": [{"class": "B", "story": "x", "cites": ["a"], "tags": "easiest_win"}]}}},
            {"students": {"9001": {"stories": [{"class": "B", "story": "x", "cites": ["a"], "tags": ["easiest_win"] * 2}]}}},
            {"students": {"9001": {"stories": [{"class": "B", "story": "x", "cites": ["a"], "tags": ["Easiest win"]}]}}},
        ]
        result = run_worker([json.dumps(doc) for doc in raw])
        self.assertEqual(len(result["failures"]), len(raw))


class PayloadRuleTests(unittest.TestCase):
    STORY = {"class": "Math", "story": "Still a B.", "cites": ["a"]}

    def doc(self, ups):
        return runner.insights_doc(ups)["students"]

    def test_student_keys_are_ascii_letters_digits_and_dashes_only(self):
        ups = [{"sn": key, "stories": [self.STORY]} for key in ("ab_c", "k" * 33, "٩٠٠١", "", "a b", "9001", "k" * 32)]
        self.assertEqual(list(self.doc(ups)), ["9001", "k" * 32])
        self.assertEqual(list(self.doc([{"sn": 9001, "stories": [self.STORY]}])), ["9001"])
        self.assertEqual(self.doc([{"sn": True, "stories": [self.STORY]}]), {})

    def test_blank_means_blank_to_js_trim(self):
        for blank in ["\ufeff", "\u00a0", "\u2028\u2029", "\u1680", "\u2000\u200a", "\u202f", "\u205f", "\u3000", " \t"]:
            up = {"sn": "9001", "as_of": blank,
                  "stories": [self.STORY, {"class": blank, "story": "x", "cites": ["a"]},
                              {"class": "B", "story": blank, "cites": ["a"]},
                              {"class": "B", "story": "x", "cites": [blank]}]}
            kid = self.doc([up])["9001"]
            self.assertEqual(kid, {"as_of": None, "stories": [self.STORY]}, repr(blank))
            self.assertFalse(insights_writer._fits_text(blank, 600), repr(blank))
        # Python strips "\x85" but JS trim keeps it, so it is text to the Worker.
        self.assertEqual(self.doc([{"sn": "9001", "stories": [{**self.STORY, "story": "\x85"}]}])["9001"]["stories"][0]
                         ["story"], "\x85")
        self.assertTrue(insights_writer._fits_text("\x85", 600))

    def test_a_lone_surrogate_never_fits_and_never_crashes(self):
        self.assertFalse(insights_writer._fits_text("ok \ud83d", 600))
        self.assertEqual(insights_writer._js_len("ok \ud83d"), 4)
        self.assertEqual(insights_writer._js_len("\U0001F600"), 2)
        self.assertFalse(insights_writer.within_publish_limits("ok", ["\udc00"]))
        ups = [{"sn": "9001", "stories": [self.STORY, {**self.STORY, "story": "ok \ud83d"},
                                          {**self.STORY, "cites": ["\ud83d"]}]}]
        self.assertEqual(self.doc(ups)["9001"]["stories"], [self.STORY])

    def test_a_student_with_nothing_left_is_left_out_and_nothing_is_sent(self):
        opener_calls = []
        publisher = runner.InsightsPublisher(secret=lambda: "standin", opener=lambda r: opener_calls.append(r) or (200, b""))
        self.assertIs(publisher.put([{"sn": "9001", "stories": [{"class": "B", "story": "x" * 700, "cites": ["a"]}]}]),
                      False)
        self.assertEqual(opener_calls, [])
        kept = self.doc([{"sn": "9001", "stories": [{"class": "B", "story": "x" * 700, "cites": ["a"]}]},
                         {"sn": "9002", "stories": [self.STORY]}])
        self.assertEqual(list(kept), ["9002"])

    def test_the_body_cap_drops_the_largest_students_last_stories_first(self):
        ups = full_ups()
        doc = runner.insights_doc(ups)
        body = runner.insights_body(doc)
        self.assertLessEqual(len(body), runner.PUBLISH_MAX_BYTES)
        self.assertGreater(len(body), runner.PUBLISH_MAX_BYTES - 3000)
        self.assertEqual(list(doc["students"]), [up["sn"] for up in ups])
        for up in ups:
            kept = doc["students"][up["sn"]]["stories"]
            self.assertEqual(kept, [{k: s[k] for k in ("class", "story", "cites", "tags")} for s in up["stories"][:len(kept)]])
        counts = [len(doc["students"][up["sn"]]["stories"]) for up in ups]
        self.assertLessEqual(max(counts) - min(counts), 1)
        self.assertEqual(runner.insights_body(runner.insights_doc(full_ups())), body)
        # What the publisher sends is exactly that body.
        sent = []
        runner.InsightsPublisher(secret=lambda: "standin", opener=lambda r: sent.append(r.data) or (200, b"")).put(ups)
        self.assertEqual(sent, [body])

    def test_no_more_than_eight_students_or_forty_stories(self):
        ups = [{"sn": f"9{i:03d}", "stories": [self.STORY] * 45} for i in range(10)]
        ups.append({"sn": "9100", "stories": [{"class": "B", "story": "x" * 700, "cites": ["a"]}]})  # nothing left
        with mock.patch("sys.stderr") as err:
            doc = self.doc(ups)
        self.assertEqual(len(doc), 8)
        self.assertTrue(all(len(kid["stories"]) == 40 for kid in doc.values()))
        # A count-only warning: no student keys, names or scores.
        logged = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertEqual(logged, "grade-insights: publish keeps 8 students; 2 more left out\n")
        with mock.patch("sys.stderr") as err:
            self.doc(ups[:8])
        self.assertEqual(err.write.call_args_list, [])

    def test_tags_are_kept_only_from_the_fixed_set_and_a_headline_never_goes(self):
        for tags, kept in ((["easiest_win"], ["easiest_win"]), (["biggest_risk", "easiest_win"], ["biggest_risk", "easiest_win"]),
                           ([], None), (None, None), ("easiest_win", None), (["easiest_win", "easiest_win"], None),
                           (["Easiest win"], None), (["top_focus"], None), ([7], None), ({"easiest_win": 1}, None)):
            got = self.doc([{"sn": "9001", "headline": {"text": "Old.", "cites": ["w"]},
                             "stories": [{**self.STORY, "tags": tags}]}])["9001"]
            self.assertEqual(got, {"as_of": None, "stories": [{**self.STORY, "tags": kept} if kept else self.STORY]},
                             repr(tags))
        # A student with only an old headline has nothing to publish.
        self.assertEqual(self.doc([{"sn": "9001", "headline": {"text": "Old.", "cites": ["w"]}, "stories": []}]), {})

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_real_worker_keeps_the_tags(self):
        doc = runner.insights_doc([{"sn": "9001", "stories": [{**self.STORY, "tags": ["easiest_win", "biggest_risk"]},
                                                              {**self.STORY, "class": "Art", "tags": ["biggest_risk"]}]}])
        self.assertEqual(doc["students"]["9001"]["stories"][0]["tags"], ["easiest_win", "biggest_risk"])
        self.assertEqual(run_worker([runner.insights_body(doc).decode()]), {"checked": 1, "failures": []})

    def test_a_body_still_over_the_cap_is_dropped_not_raised(self):
        ups = [{"sn": "9001", "stories": [self.STORY]}]
        with mock.patch.object(runner, "insights_body", return_value=b"x" * (runner.PUBLISH_MAX_BYTES + 1)):
            self.assertEqual(runner.insights_doc(ups), {"students": {}})


if __name__ == "__main__":
    unittest.main()

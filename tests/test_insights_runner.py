import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import insights_runner as runner  # noqa: E402

PT = runner.PACIFIC
EVENING_STAMP = "2026-10-23T03:07:40.000Z"  # Thu Oct 22, 8:07 PM PDT
AFTERNOON_STAMP = "2026-10-22T23:45:30.000Z"  # Thu Oct 22, 4:45 PM PDT


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=PT)


class FakeWork:
    def __init__(self, steps, fail_on=None):
        self.steps = steps
        self.ran = []
        self.fail_on = fail_on

    def plan(self, night):
        return list(self.steps)

    def run_step(self, night, step):
        if step == self.fail_on:
            self.fail_on = None
            raise RuntimeError("model crashed")
        self.ran.append(step)


class FakeMachine:
    def __init__(self, now, stamp=EVENING_STAMP, ssd=True, memory=80, busy=False):
        self.clock = now
        self.stamp = stamp
        self.ssd = ssd
        self.memory = memory
        self.is_busy = busy

    def machine(self):
        return runner.Machine(
            now=lambda: self.clock,
            freshness=lambda: self.stamp,
            ssd_mounted=lambda: self.ssd,
            free_memory_pct=lambda: self.memory,
            busy=lambda: self.is_busy,
        )


class WindowTests(unittest.TestCase):
    def test_hard_stops_on_the_pacific_clock(self):
        cases = {
            at(22, 20, 15): False,
            at(23, 0, 30): False,
            at(23, 3, 4): False,
            at(23, 3, 5): True,
            at(23, 4, 30): True,
            at(23, 4, 40): False,
            at(23, 6, 30): True,
            at(23, 12, 0): True,
            at(25, 0, 59): False,
            at(25, 1, 0): True,
            at(25, 5, 0): True,
        }
        self.assertEqual({t: runner.blocked(t) for t in cases}, cases)

    def test_only_the_evening_scrape_starts_a_night(self):
        self.assertEqual(runner.evening_night(EVENING_STAMP), "2026-10-22")
        self.assertIsNone(runner.evening_night(AFTERNOON_STAMP))
        self.assertIsNone(runner.evening_night("2026-10-23T09:07:10.000Z"))
        self.assertEqual(runner.evening_night("2026-11-03T03:07:00.000Z"), "2026-11-02")

    def test_after_midnight_belongs_to_the_previous_night(self):
        self.assertEqual(runner.tonight(at(23, 2, 0)), "2026-10-22")
        self.assertEqual(runner.tonight(at(22, 20, 0)), "2026-10-22")


class TickTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state.json"
        self.logs = []

    def tick(self, work, fake):
        return runner.tick(runner.State(self.path), work, fake.machine(), log=self.logs.append)

    def test_waits_until_the_evening_scrape_lands(self):
        work = FakeWork(["a"])
        fake = FakeMachine(at(22, 19, 30), stamp=AFTERNOON_STAMP)
        self.assertEqual(self.tick(work, fake), "waiting_for_scrape")
        fake.stamp = EVENING_STAMP
        fake.clock = at(22, 20, 10)
        self.assertEqual(self.tick(work, fake), "done")
        self.assertEqual(work.ran, ["a"])

    def test_missing_ssd_skips_the_whole_night(self):
        work = FakeWork(["a"])
        fake = FakeMachine(at(22, 20, 10), ssd=False)
        self.assertEqual(self.tick(work, fake), "skipped_no_ssd")
        fake.ssd = True
        fake.clock = at(22, 21, 0)
        self.assertEqual(self.tick(work, fake), "skipped_no_ssd")
        self.assertEqual(work.ran, [])
        self.assertEqual(self.logs, ["grade-insights: 2026-10-22 skipped, the SSD is not mounted"])

    def test_pauses_at_the_backup_stop_and_resumes_after_the_photo_drop_clear(self):
        fake = FakeMachine(at(22, 20, 10))
        work = FakeWork(["a", "b", "c"])
        steps = iter([at(22, 20, 10), at(23, 3, 4), at(23, 3, 5)])
        machine = fake.machine()
        moving = runner.Machine(
            now=lambda: next(steps), freshness=machine.freshness, ssd_mounted=machine.ssd_mounted,
            free_memory_pct=machine.free_memory_pct, busy=machine.busy,
        )
        self.assertEqual(runner.tick(runner.State(self.path), work, moving, log=self.logs.append), "paused")
        self.assertEqual(work.ran, ["a"])
        fake.clock = at(23, 4, 0)
        self.assertEqual(self.tick(work, fake), "blocked")
        fake.clock = at(23, 4, 40)
        self.assertEqual(self.tick(work, fake), "done")
        self.assertEqual(work.ran, ["a", "b", "c"])

    def test_waits_for_a_render_or_low_memory(self):
        work = FakeWork(["a"])
        fake = FakeMachine(at(22, 20, 10), busy=True)
        self.assertEqual(self.tick(work, fake), "waiting_for_machine")
        fake.is_busy = False
        fake.memory = 20
        self.assertEqual(self.tick(work, fake), "waiting_for_machine")
        fake.memory = 60
        self.assertEqual(self.tick(work, fake), "done")
        self.assertEqual(work.ran, ["a"])

    def test_a_crash_mid_step_reruns_only_that_step(self):
        work = FakeWork(["a", "b", "c"], fail_on="b")
        fake = FakeMachine(at(22, 20, 10))
        with self.assertRaises(RuntimeError):
            self.tick(work, fake)
        self.assertEqual(runner.State(self.path).get("done"), ["a"])
        self.assertEqual(self.tick(work, fake), "done")
        self.assertEqual(work.ran, ["a", "b", "c"])

    def test_a_done_night_does_not_run_again_and_the_next_night_starts_fresh(self):
        work = FakeWork(["a"])
        fake = FakeMachine(at(22, 20, 10))
        self.assertEqual(self.tick(work, fake), "done")
        fake.clock = at(22, 23, 0)
        self.assertEqual(self.tick(work, fake), "done")
        self.assertEqual(work.ran, ["a"])
        fake.clock = at(23, 20, 10)
        fake.stamp = "2026-10-24T03:07:00.000Z"
        self.assertEqual(self.tick(work, fake), "done")
        self.assertEqual(work.ran, ["a", "a"])
        self.assertEqual(runner.State(self.path).get("night"), "2026-10-23")

    def test_saturday_night_stops_before_amazon_photos(self):
        work = FakeWork(["a"])
        fake = FakeMachine(at(25, 1, 30), stamp="2026-10-25T03:07:00.000Z")
        self.assertEqual(self.tick(work, fake), "blocked")
        self.assertEqual(work.ran, [])

    def test_pending_work_finishes_a_night_with_nothing_to_do(self):
        fake = FakeMachine(at(22, 20, 10))
        self.assertEqual(self.tick(runner.PendingWork(), fake), "done")


class KeychainTests(unittest.TestCase):
    def test_returns_the_value_without_the_newline(self):
        done = mock.Mock(returncode=0, stdout="s3cr3t-value\n")
        with mock.patch.object(runner.subprocess, "run", return_value=done) as run:
            self.assertEqual(runner.keychain_secret("grade-insights", "family-data"), "s3cr3t-value")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/security", "find-generic-password", "-s", "grade-insights", "-a", "family-data", "-w"],
        )

    def test_missing_item_raises_without_any_value(self):
        failed = mock.Mock(returncode=44, stdout="", stderr="could not be found")
        with mock.patch.object(runner.subprocess, "run", return_value=failed):
            with self.assertRaises(runner.KeychainError) as caught:
                runner.keychain_secret("grade-insights", "family-data")
        self.assertEqual(str(caught.exception), "no Keychain item for service 'grade-insights' (exit 44)")


    def test_parent_pin_reads_the_family_data_item(self):
        done = mock.Mock(returncode=0, stdout="0000\n")
        with mock.patch.object(runner.subprocess, "run", return_value=done) as run:
            self.assertEqual(runner.parent_pin(), "0000")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/security", "find-generic-password", "-s", "family-data-parent-pin", "-a", "aeries", "-w"],
        )

    def test_a_tick_never_reads_the_keychain(self):
        fake = FakeMachine(at(22, 20, 10))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner, "keychain_secret") as secret:
            status = runner.tick(runner.State(Path(tmp) / "state.json"), runner.PendingWork(), fake.machine())
        self.assertEqual(status, "done")
        secret.assert_not_called()


class ModelPathTests(unittest.TestCase):
    def test_models_and_hf_cache_sit_on_the_ssd(self):
        self.assertEqual(runner.model_dir(runner.DEFAULT_SSD), Path("/Volumes/David C SSD/grade-insights"))
        env = runner.model_env(runner.DEFAULT_SSD, base={"PATH": "/usr/bin", "HF_HOME": "/Users/x/.cache"})
        self.assertEqual(env, {
            "PATH": "/usr/bin",
            "HF_HOME": "/Volumes/David C SSD/grade-insights/hf-cache",
            "HF_HUB_OFFLINE": "1",
        })
        self.assertEqual(runner.venv_python(runner.DEFAULT_SSD),
                         Path("/Volumes/David C SSD/grade-insights/venv/bin/python"))
        self.assertEqual(runner.model_link(runner.DEFAULT_SSD, "qwen3-8b"),
                         Path("/Volumes/David C SSD/grade-insights/models/Qwen3-8B-4bit"))
        self.assertEqual(runner.model_link(runner.DEFAULT_SSD, "gemma3-12b").name, "gemma-3-text-12b-it-4bit")
        self.assertEqual(sorted(runner.MODEL_REPOS.values()),
                         ["mlx-community/Qwen3-8B-4bit", "mlx-community/gemma-3-text-12b-it-4bit"])


class ReadOnlyClientTests(unittest.TestCase):
    """The PIN session has the write role; the runner may only GET with it."""

    def client(self, responses):
        self.sent = []
        replies = iter(responses)

        def opener(request):
            self.sent.append(request)
            return next(replies)

        return runner.ReadOnlyFamilyData(pin=lambda: "0000", opener=opener)

    def assert_only_gets_and_one_unlock(self):
        for request in self.sent:
            if request.get_method() != "GET":
                self.assertEqual((request.get_method(), request.full_url),
                                 ("POST", runner.FAMILY_DATA_URL + "/auth/unlock"))

    def test_reads_unlock_once_then_get(self):
        client = self.client([(200, b'{"token": "t1"}'), (200, b'{"students": []}'), (200, b'{"version": 3}')])
        self.assertEqual(client.latest_grades(), {"students": []})
        self.assertEqual(client.scored_log(), {"version": 3})
        self.assertEqual([r.get_method() for r in self.sent], ["POST", "GET", "GET"])
        self.assertEqual(self.sent[1].get_header("Authorization"), "Bearer t1")
        self.assert_only_gets_and_one_unlock()

    def test_an_expired_session_unlocks_again(self):
        client = self.client([(200, b'{"token": "t1"}'), (401, b""), (200, b'{"token": "t2"}'), (200, b"{}")])
        client.latest_grades()
        self.assertEqual([r.get_method() for r in self.sent], ["POST", "GET", "POST", "GET"])
        self.assert_only_gets_and_one_unlock()

    def test_a_second_401_raises_after_one_fresh_unlock(self):
        client = self.client([(200, b'{"token": "t1"}'), (401, b""), (200, b'{"token": "t2"}'), (401, b"")])
        with self.assertRaises(runner.FamilyDataError) as caught:
            client.latest_grades()
        self.assertIn("401", str(caught.exception))
        self.assertEqual([r.get_method() for r in self.sent], ["POST", "GET", "POST", "GET"])

    def _two_servers(self):
        """A family-data stand-in that redirects to a second server on another port. Both record hits."""
        import http.server
        import threading

        servers, threads, hits = [], [], {"origin": [], "elsewhere": []}

        def handler(name, redirect_to=None):
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    hits[name].append((self.path, self.headers.get("Authorization")))
                    if redirect_to:
                        self.send_response(302)
                        self.send_header("Location", f"http://127.0.0.1:{redirect_to()}/collect")
                        self.end_headers()
                    else:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"{}")

                def log_message(self, *args):
                    pass

            return Handler

        elsewhere = http.server.HTTPServer(("127.0.0.1", 0), handler("elsewhere"))
        origin = http.server.HTTPServer(("127.0.0.1", 0), handler("origin", lambda: elsewhere.server_port))
        for server in (origin, elsewhere):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)

        def stop():
            for server, thread in zip(servers, threads):
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertNotEqual(origin.server_port, elsewhere.server_port)
        return origin, hits, stop

    def _token_request(self, origin):
        return runner.urllib.request.Request(
            f"http://127.0.0.1:{origin.server_port}/v1/grades/latest",
            headers={"Authorization": "Bearer synthetic-token"})

    def test_the_session_opener_refuses_redirects(self):
        """A redirect to another server must not reach it, so the bearer token can't follow."""
        origin, hits, stop = self._two_servers()
        try:
            status, body = runner._open_request(self._token_request(origin))
        finally:
            stop()
        self.assertEqual(status, 302)
        self.assertEqual(body, b"")
        self.assertEqual(hits["origin"], [("/v1/grades/latest", "Bearer synthetic-token")])
        self.assertEqual(hits["elsewhere"], [])
        self.assertFalse(any(auth for _, auth in hits["elsewhere"]))

    def test_the_two_server_check_would_catch_a_following_opener(self):
        """Control: urllib's default opener does follow, and the second server sees the request."""
        origin, hits, stop = self._two_servers()
        try:
            with runner.urllib.request.build_opener().open(self._token_request(origin), timeout=10) as response:
                response.read()
        finally:
            stop()
        self.assertEqual([path for path, _ in hits["elsewhere"]], ["/collect"])

    def test_a_redirect_is_an_error_not_a_follow(self):
        client = self.client([(200, b'{"token": "t1"}'), (302, b"")])
        with self.assertRaises(runner.FamilyDataError):
            client.latest_grades()
        self.assertEqual([r.get_method() for r in self.sent], ["POST", "GET"])

    def test_no_write_methods_exist(self):
        public = {name.lower() for name in dir(runner.ReadOnlyFamilyData) if not name.startswith("_")}
        self.assertEqual(public & {"put", "post", "patch", "delete", "request", "send", "publish", "write"}, set())

    def test_any_other_non_get_is_refused_before_it_leaves(self):
        client = self.client([])
        for method, path in (("PUT", "/v1/docs/aeries/scored-log"), ("POST", "/v1/grades"),
                             ("DELETE", "/v1/docs/aeries/x"), ("PATCH", "/v1/grades"), ("POST", "/auth/unlock/x")):
            with self.assertRaises(runner.FamilyDataError):
                client._send(method, path, {})
        self.assertEqual(self.sent, [])

    def test_errors_carry_no_secret(self):
        client = self.client([(200, b'{"token": "s3cr3t-token"}'), (500, b"")])
        with self.assertRaises(runner.FamilyDataError) as caught:
            client.latest_grades()
        self.assertNotIn("s3cr3t-token", str(caught.exception))
        self.assertNotIn("0000", str(caught.exception))

    def test_the_runner_source_sends_nothing_else(self):
        """Fails if any code path in the runner gains another non-GET."""
        source = Path(runner.__file__).read_text()
        self.assertNotRegex(source, r"\b(PATCH|DELETE)\b")
        self.assertEqual(re.findall(r'"POST"', source), ['"POST"', '"POST"'])
        # The one PUT is InsightsPublisher's, to the insights path only.
        self.assertEqual(re.findall(r'"PUT"', source), ['"PUT"'])
        publisher = source[source.index("class InsightsPublisher"):source.index("def insights_doc")]
        self.assertIn('method="PUT"', publisher)
        self.assertIn("self._base + INSIGHTS_PATH", publisher)
        self.assertNotIn("requests", re.findall(r"^import (\w+)", source, re.M))
        self.assertEqual(re.findall(r"urlopen\(", source), ["urlopen("])
        self.assertEqual(re.findall(r"\.open\(", source), [".open("])

    def test_a_tick_sends_only_gets(self):
        """End to end through tick and the real opener: every request is a GET."""
        import io
        seen = []

        def urlopen(request, timeout=None):
            seen.append(request.get_method())
            return io.BytesIO(b'{"updatedAt": "2026-10-22T03:08:36Z"}')

        fake = FakeMachine(at(22, 20, 10))
        machine = runner.Machine(now=fake.machine().now, freshness=runner.read_freshness,
                                 ssd_mounted=lambda: True, free_memory_pct=lambda: 80, busy=lambda: False)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner.urllib.request, "urlopen", side_effect=urlopen):
            runner.tick(runner.State(Path(tmp) / "state.json"), runner.PendingWork(), machine)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"GET"})


def synthetic_latest():
    """Two synthetic students, Kid A with a Math what-if and Kid B with no facts. No real data."""
    whatif = {"id": "whatif:math:tests:50", "kind": "what_if", "class": "Math", "bucket": "Tests",
              "bucket_weight_pct": 60.0, "points": 50.0, "target": 90.0, "needed_pct": 88.0, "reachable": True}
    kid_a = {
        "sn": "9001",
        "classes": [{"period": 3, "course_name": "Math", "percent": "87.9", "mark": "B", "counted_insight": {
            "layout": "percent_of_grade",
            "categories": [{"name": "Tests", "weight_pct": 60.0}, {"name": "Homework", "weight_pct": 40.0}]}}],
        "assignments_by_class": [
            {"class_name": "<<3- Math", "assignments": [
                {"description": "Ch. 4 Quiz", "category": "Tests", "points_earned": 31, "points_possible": 50,
                 "due_date": "10/14/2026"},
                {"description": "Ch. 5 Test", "category": "Tests", "points_earned": None, "points_possible": 50,
                 "due_date": "10/30/2026"},
            ]},
            {"class_name": "<<4- Art", "assignments": [
                {"description": "Sketch", "category": "Tests", "points_earned": None, "points_possible": 50,
                 "due_date": "10/24/2026"},
            ]},
        ],
        "insight_facts": {"scale": "90/80/70/60", "as_of": "2026-10-22", "facts": [whatif]},
    }
    kid_b = {"sn": "9002", "classes": [], "assignments_by_class": []}
    log = {"doc": {"schema": 1, "students": {"9001": {"items": [{"class": "Math", "first_seen": "2026-10-16"}]}}},
           "version": 4}
    return {"latest": {"students": [kid_a, kid_b]}}, log


class FakeClient:
    def __init__(self):
        self.latest, self.log = synthetic_latest()
        self.calls = []

    def latest_grades(self):
        self.calls.append("latest")
        return self.latest

    def scored_log(self):
        self.calls.append("log")
        return self.log


def writer_has_upcoming():
    try:
        import insights_writer
    except ImportError:
        return False
    return hasattr(insights_writer, "upcoming_items") and hasattr(insights_writer, "attach_next_items")


class LoaderTests(unittest.TestCase):
    def test_the_loader_passes_upcoming_items_built_from_the_class_inputs(self):
        seen = []

        def build(classes, as_of):
            seen.append((classes, as_of))
            return [{"class": "Math", "bucket": "Tests", "title": "Ch. 5 Test", "due": "2026-10-30", "points": 50.0}]

        client = FakeClient()
        students = runner.load_students(client, upcoming_items=build)
        self.assertEqual(client.calls, ["latest", "log"])
        self.assertEqual(len(students), 1)
        self.assertEqual(students[0]["upcoming"][0]["title"], "Ch. 5 Test")
        self.assertEqual(students[0]["log_items"], [{"class": "Math", "first_seen": "2026-10-16"}])
        self.assertEqual(students[0]["insight_facts"]["as_of"], "2026-10-22")
        self.assertEqual(students[0]["sn"], "9001")
        classes, as_of = seen[0]
        self.assertEqual(as_of, "2026-10-22")
        self.assertEqual([c["name"] for c in classes], ["Math"])
        self.assertEqual([a["description"] for a in classes[0]["assignments"]], ["Ch. 4 Quiz", "Ch. 5 Test"])
        self.assertEqual(classes[0]["counted_insight"]["categories"][0]["name"], "Tests")

    def test_without_the_writer_the_loader_still_loads_with_no_upcoming(self):
        with mock.patch.dict(sys.modules, {"insights_writer": None}):
            self.assertIsNone(runner.writer_upcoming())
            self.assertIsNone(runner.writer_work(FakeClient(), model=None, store="/nonexistent"))
            students = runner.load_students(FakeClient())
        self.assertEqual(students[0]["upcoming"], [])

    def _with_writer_source(self, source):
        """Put a synthetic insights_writer.py first on sys.path for one test; restore everything after."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, "insights_writer.py").write_text(source)
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        sys.modules.pop("insights_writer", None)
        path = mock.patch.object(sys, "path", [tmp.name] + sys.path)
        path.start()
        self.addCleanup(path.stop)

    def _load(self):
        lines = []
        students = runner.load_students(FakeClient(), log=lines.append)
        return students, lines

    def test_a_truly_missing_writer_is_quiet(self):
        missing = ModuleNotFoundError("No module named 'insights_writer'", name="insights_writer")
        with mock.patch.object(runner.importlib, "import_module", side_effect=missing):
            students, lines = self._load()
            work_lines = []
            self.assertIsNone(runner.writer_work(FakeClient(), None, "/nonexistent", log=work_lines.append))
        self.assertEqual(students[0]["upcoming"], [])
        self.assertEqual(lines + work_lines, [])

    def test_a_writer_that_raises_on_import_warns_once_and_the_run_continues(self):
        self._with_writer_source("SECRET_MARKER = 1\nraise RuntimeError('synthetic token value')\n")
        students, lines = self._load()
        self.assertEqual(len(students), 1)
        self.assertEqual(students[0]["upcoming"], [])
        self.assertEqual(lines, ["grade-insights: warning: writer upcoming_items unavailable (RuntimeError); "
                                 "0 upcoming items"])
        self.assertNotIn("synthetic token value", lines[0])

    def test_other_import_failures_warn_once_with_the_type_only(self):
        cases = {
            "import not_a_real_module_for_tests\n": "ModuleNotFoundError",
            "def broken(:\n": "SyntaxError",
            "from os import not_a_real_name\n": "ImportError",
        }
        for source, kind in cases.items():
            with self.subTest(kind=kind):
                self._with_writer_source(source)
                students, lines = self._load()
                self.assertEqual(students[0]["upcoming"], [])
                self.assertEqual(len(lines), 1)
                self.assertIn(f"({kind})", lines[0])
                self.assertNotIn("not_a_real", lines[0])
                self.doCleanups()

    def test_a_writer_missing_the_name_warns_once(self):
        fake = types.ModuleType("insights_writer")
        with mock.patch.dict(sys.modules, {"insights_writer": fake}):
            students, lines = self._load()
            work_lines = []
            self.assertIsNone(runner.writer_work(FakeClient(), None, "/nonexistent", log=work_lines.append))
        self.assertEqual(students[0]["upcoming"], [])
        self.assertEqual(lines, ["grade-insights: warning: writer upcoming_items unavailable (ImportError); "
                                 "0 upcoming items"])
        self.assertEqual(work_lines, ["grade-insights: warning: writer WriterWork unavailable (ImportError); "
                                      "0 upcoming items"])

    def test_the_loader_reads_with_one_unlock_and_gets_only(self):
        latest, log = synthetic_latest()
        sent = []
        replies = iter([(200, b'{"token": "t1"}'), (200, json.dumps(latest).encode()),
                        (200, json.dumps(log).encode())])

        def opener(request):
            sent.append(request.get_method())
            return next(replies)

        client = runner.ReadOnlyFamilyData(pin=lambda: "0000", opener=opener)
        students = runner.load_students(client, upcoming_items=lambda classes, as_of: [])
        self.assertEqual(sent, ["POST", "GET", "GET"])
        self.assertEqual(len(students), 1)

    @unittest.skipUnless(writer_has_upcoming(), "needs the writer's upcoming_items (PR #52)")
    def test_a_what_if_gets_next_title_and_next_due_from_the_loader(self):
        import insights_writer

        student = runner.load_students(FakeClient())[0]
        self.assertEqual(student["upcoming"], [
            {"class": "Math", "bucket": "Tests", "title": "Ch. 5 Test", "due": "2026-10-30", "points": 50.0}])
        facts = insights_writer.attach_next_items(student["insight_facts"]["facts"], student["upcoming"])
        whatif = next(f for f in facts if f["kind"] == "what_if")
        self.assertEqual((whatif["next_title"], whatif["next_due"]), ("Ch. 5 Test", "2026-10-30"))

    @unittest.skipUnless(writer_has_upcoming(), "needs the writer's WriterWork (PR #52)")
    def test_writer_work_hands_the_model_the_named_item_and_loads_once(self):
        drafts = []

        class Model:
            def complete(self, system, user):
                # The draft prompt's marker (#52's DRAFT_SYSTEM says "You get FACTS as JSON."). "class story"
                # left #52 at fcec92e, so it must not be part of the filter.
                if "FACTS as JSON" in system:
                    drafts.append(json.loads(user))
                return json.dumps({"sentences": []})

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            work = runner.writer_work(client, Model(), tmp, env={})
            for step in work.plan("2026-10-22"):
                work.run_step("2026-10-22", step)
        self.assertEqual(client.calls, ["latest", "log"])
        whatif = next(f for f in drafts[0]["facts"] if f["kind"] == "what_if")
        self.assertEqual((whatif["next_title"], whatif["next_due"]), ("Ch. 5 Test", "2026-10-30"))


SECRET_STANDIN = "insights-secret-standin-not-real"


class FakeOpener:
    def __init__(self, status=200):
        self.status, self.requests = status, []

    def __call__(self, request):
        self.requests.append(request)
        return self.status, b'{"ok": true}'


def worker_accepts(body):
    """A mirror of family-data cleanInsights (src/insights.js): True when the Worker would take it."""
    def js_len(text):
        return len(text.encode("utf-16-le")) // 2

    def text_ok(value, limit):
        return isinstance(value, str) and value.strip() != "" and js_len(value) <= limit

    def line_ok(value, field):
        cites = value.get("cites") or []
        return (isinstance(value, dict) and text_ok(value.get(field), 600) and len(cites) <= 12
                and all(text_ok(c, 120) for c in cites))

    students = body.get("students")
    if not isinstance(students, dict) or len(students) > 8:
        return False
    for key, up in students.items():
        if not re.fullmatch(r"[A-Za-z0-9-]{1,32}", key) or len(up.get("stories") or []) > 40:
            return False
        if up.get("as_of") is not None and not text_ok(up["as_of"], 40):
            return False
        if "headline" in up:
            return False
        if not all(line_ok(s, "story") and text_ok(s.get("class"), 120) for s in up.get("stories") or []):
            return False
    return True


class InsightsPublisherTests(unittest.TestCase):
    """PR5's write path. The network is always a fake: no real PUT ever leaves a test."""

    UPS = [
        {"sn": "9001", "as_of": "2026-10-22",
         "stories": [{"class": "Math", "story": "Still a B, 2.1 points from an A.", "cites": ["gap:math"],
                      "tags": ["easiest_win"], "dropped": ["kept local"]}]},
        {"as_of": "2026-10-22", "stories": []},
    ]

    def test_the_keychain_item_is_its_own_service(self):
        self.assertEqual(runner.INSIGHTS_KEYCHAIN_SERVICE, "family-data-insights-write")
        self.assertNotEqual(runner.INSIGHTS_KEYCHAIN_SERVICE, runner.PIN_KEYCHAIN_SERVICE)
        with mock.patch.object(runner, "keychain_secret", return_value=SECRET_STANDIN) as read:
            self.assertEqual(runner.insights_secret(), SECRET_STANDIN)
        read.assert_called_once_with("family-data-insights-write", "aeries")

    def test_put_sends_one_bearer_put_to_the_insights_path_only(self):
        opener = FakeOpener()
        reads = []
        publisher = runner.InsightsPublisher(secret=lambda: reads.append(1) or SECRET_STANDIN,
                                             base="https://family-data.example/", opener=opener)
        self.assertEqual(reads, [])
        self.assertTrue(publisher.put(self.UPS))
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.full_url, "https://family-data.example/v1/grades/insights")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {SECRET_STANDIN}")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(list(body["students"]), ["9001"])
        self.assertEqual(body["students"]["9001"]["stories"],
                         [{"class": "Math", "story": "Still a B, 2.1 points from an A.", "cites": ["gap:math"],
                           "tags": ["easiest_win"]}])
        self.assertNotIn("kept local", request.data.decode("utf-8"))
        self.assertEqual(reads, [1])

    def test_a_failed_put_reports_the_status_only(self):
        publisher = runner.InsightsPublisher(secret=lambda: SECRET_STANDIN, opener=FakeOpener(status=404))
        with self.assertRaises(runner.FamilyDataError) as caught:
            publisher.put(self.UPS)
        self.assertEqual(str(caught.exception), "PUT /v1/grades/insights returned 404")
        self.assertNotIn(SECRET_STANDIN, str(caught.exception))

    def test_a_redirect_is_never_followed(self):
        """Through the real opener and _RefuseRedirects: a 302 is an error and the target gets nothing."""
        import http.server
        import threading

        hits = {"origin": [], "elsewhere": []}

        def handler(name, redirect_to=None):
            class Handler(http.server.BaseHTTPRequestHandler):
                def _any(self):
                    length = int(self.headers.get("Content-Length") or 0)
                    if length:
                        self.rfile.read(length)
                    hits[name].append((self.command, self.path, self.headers.get("Authorization")))
                    if redirect_to:
                        self.send_response(302)
                        self.send_header("Location", f"http://127.0.0.1:{redirect_to()}/collect")
                    else:
                        self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

                do_PUT = do_GET = do_POST = _any

                def log_message(self, *args):
                    pass

            return Handler

        elsewhere = http.server.HTTPServer(("127.0.0.1", 0), handler("elsewhere"))
        origin = http.server.HTTPServer(("127.0.0.1", 0), handler("origin", lambda: elsewhere.server_port))
        threads = [threading.Thread(target=srv.serve_forever, daemon=True) for srv in (origin, elsewhere)]
        for thread in threads:
            thread.start()
        try:
            publisher = runner.InsightsPublisher(secret=lambda: SECRET_STANDIN,
                                                 base=f"http://127.0.0.1:{origin.server_port}")
            with self.assertRaises(runner.FamilyDataError) as caught:
                publisher.put(self.UPS)
        finally:
            for srv, thread in zip((origin, elsewhere), threads):
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5)
        self.assertEqual(str(caught.exception), "PUT /v1/grades/insights returned 302")
        self.assertEqual(hits["origin"], [("PUT", "/v1/grades/insights", f"Bearer {SECRET_STANDIN}")])
        self.assertEqual(hits["elsewhere"], [])

    def test_no_student_key_means_no_put_at_all(self):
        opener = FakeOpener()
        reads = []
        publisher = runner.InsightsPublisher(secret=lambda: reads.append(1) or SECRET_STANDIN, opener=opener)
        for ups in ([{"stories": []}], [], None, [{"sn": "", "stories": []}, {"as_of": "2026-10-22"}]):
            self.assertIs(publisher.put(ups), False)
        self.assertEqual(opener.requests, [])
        self.assertEqual(reads, [])

    def test_the_payload_is_capped_and_deduped_right_before_the_put(self):
        cites12 = [f"fact:{i}" for i in range(12)]
        ups = [{"sn": "9001", "as_of": "2026-10-22",
                "headline": {"text": "h" * 601, "cites": ["fact:0"]},
                "stories": [
                    {"class": "Math", "story": "Dup cites.", "cites": ["a", "b", "a", "b", "a"]},
                    {"class": "Art", "story": "No cites."},
                    {"class": "Art", "story": "Empty cites.", "cites": []},
                    {"class": "Biology", "story": "Too many.", "cites": cites12 + ["fact:12"]},
                    {"class": "Biology", "story": "x" * 601, "cites": ["fact:0"]},
                    {"class": "Chem", "story": "y" * 600, "cites": cites12 + cites12},
                    {"class": "Spanish", "story": "Long cite.", "cites": ["c" * 121]},
                ]}]
        opener = FakeOpener()
        runner.InsightsPublisher(secret=lambda: SECRET_STANDIN, opener=opener).put(ups)
        body = json.loads(opener.requests[0].data.decode("utf-8"))
        kid = body["students"]["9001"]
        self.assertNotIn("headline", kid)
        # Sorted by class name before publishing.
        self.assertEqual(kid["stories"], [
            {"class": "Chem", "story": "y" * 600, "cites": cites12},
            {"class": "Math", "story": "Dup cites.", "cites": ["a", "b"]},
        ])
        self.assertTrue(worker_accepts(body))

    def test_the_guard_holds_in_behavior_too(self):
        client = runner.ReadOnlyFamilyData(pin=lambda: "0000", opener=FakeOpener())
        for method in ("PUT", "PATCH", "DELETE", "POST"):
            with self.assertRaises(runner.FamilyDataError):
                client._send(method, "/v1/grades/insights", {})
        opener = FakeOpener()
        runner.InsightsPublisher(secret=lambda: SECRET_STANDIN, opener=opener).put(self.UPS)
        self.assertEqual([(r.get_method(), r.full_url.split("/", 3)[3]) for r in opener.requests],
                         [("PUT", "v1/grades/insights")])

    def test_publishing_is_off_unless_the_flag_is_exactly_1(self):
        fake = runner.InsightsPublisher(secret=lambda: SECRET_STANDIN, opener=FakeOpener())
        for env in ({}, {"GRADE_INSIGHTS_PUBLISH": ""}, {"GRADE_INSIGHTS_PUBLISH": "0"},
                    {"GRADE_INSIGHTS_PUBLISH": "true"}):
            self.assertIsNone(runner.publisher_put(env, fake), env)
        self.assertEqual(runner.publisher_put({"GRADE_INSIGHTS_PUBLISH": "1"}, fake), fake.put)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(runner.publisher_put())

    @unittest.skipUnless(writer_has_upcoming(), "needs the writer's WriterWork (PR #52)")
    def test_the_nightly_publish_step_puts_the_write_ups_keyed_by_student(self):
        class Model:
            def complete(self, system, user):
                return json.dumps({"sentences": []})

        import insights_writer

        def one_story(model, facts, log_items=None, votes=None, upcoming=None):
            return {"as_of": facts["as_of"], "stories": [
                {"class": "Math", "story": "Still a B, 2.1 points from an A.", "cites": ["whatif:math:tests:50"],
                 "slotted": "x", "dropped": ["vote"]}]}

        opener = FakeOpener()
        fake = runner.InsightsPublisher(secret=lambda: SECRET_STANDIN, opener=opener)
        env = {"GRADE_INSIGHTS_PUBLISH": "1"}
        lines = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(insights_writer, "write_up", one_story):
            work = runner.writer_work(FakeClient(), Model(), tmp, put=runner.publisher_put(env, fake), env=env,
                                      log=lines.append)
            for step in work.plan("2026-10-22"):
                work.run_step("2026-10-22", step)
        self.assertEqual(len(opener.requests), 1)
        body = json.loads(opener.requests[0].data.decode("utf-8"))
        self.assertEqual(list(body["students"]), ["9001"])
        self.assertEqual(body["students"]["9001"]["as_of"], "2026-10-22")
        self.assertFalse(any(SECRET_STANDIN in line for line in lines))

    @unittest.skipUnless(writer_has_upcoming(), "needs the writer's WriterWork (PR #52)")
    def test_a_night_with_no_surviving_lines_sends_nothing(self):
        class Model:
            def complete(self, system, user):
                return json.dumps({"sentences": []})

        opener = FakeOpener()
        fake = runner.InsightsPublisher(secret=lambda: SECRET_STANDIN, opener=opener)
        env = {"GRADE_INSIGHTS_PUBLISH": "1"}
        with tempfile.TemporaryDirectory() as tmp:
            work = runner.writer_work(FakeClient(), Model(), tmp, put=runner.publisher_put(env, fake), env=env)
            for step in work.plan("2026-10-22"):
                work.run_step("2026-10-22", step)
            published = json.loads((Path(tmp) / "2026-10-22" / "publish.json").read_text())
        self.assertEqual(opener.requests, [])
        self.assertEqual(published, {"published": False})

    @unittest.skipUnless(writer_has_upcoming(), "needs the writer's WriterWork (PR #52)")
    def test_with_the_flag_unset_the_publish_step_sends_nothing(self):
        class Model:
            def complete(self, system, user):
                return json.dumps({"sentences": []})

        opener = FakeOpener()
        fake = runner.InsightsPublisher(secret=lambda: SECRET_STANDIN, opener=opener)
        with tempfile.TemporaryDirectory() as tmp:
            work = runner.writer_work(FakeClient(), Model(), tmp, put=runner.publisher_put({}, fake), env={})
            for step in work.plan("2026-10-22"):
                work.run_step("2026-10-22", step)
            published = json.loads((Path(tmp) / "2026-10-22" / "publish.json").read_text())
        self.assertEqual(opener.requests, [])
        self.assertEqual(published, {"published": False})


class FreshnessTests(unittest.TestCase):
    def test_reads_updated_at_with_the_stdlib(self):
        import io
        body = io.BytesIO(b'{"ok": true, "updatedAt": "2026-10-22T03:08:36Z"}')
        with mock.patch.object(runner.urllib.request, "urlopen", return_value=body) as urlopen:
            self.assertEqual(runner.read_freshness(), "2026-10-22T03:08:36Z")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, runner.FRESHNESS_URL)
        self.assertEqual(request.get_header("User-agent"), "grade-insights-runner/1")


class SsdMountTests(unittest.TestCase):
    def check(self, mounted=True, name="David C SSD", returncode=0):
        import plistlib
        info = mock.Mock(returncode=returncode, stdout=plistlib.dumps({"VolumeName": name}))
        with mock.patch.object(runner.os.path, "ismount", return_value=mounted), \
                mock.patch.object(runner.subprocess, "run", return_value=info) as run:
            ok = runner.ssd_mounted(runner.DEFAULT_SSD)
        return ok, run

    def test_the_named_volume_passes(self):
        ok, run = self.check()
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0], ["/usr/sbin/diskutil", "info", "-plist", "/Volumes/David C SSD"])

    def test_a_plain_folder_or_another_volume_fails(self):
        self.assertFalse(self.check(mounted=False)[0])
        self.assertFalse(self.check(name="Backup")[0])
        self.assertFalse(self.check(returncode=1)[0])


class TemplateTests(unittest.TestCase):
    def test_plist_template_is_background_and_not_run_at_load(self):
        import plistlib

        path = Path(__file__).resolve().parents[1] / "deploy" / "com.davidcasas.grade-insights.plist.template"
        plist = plistlib.loads(path.read_bytes())
        self.assertEqual(
            (plist["Label"], plist["StartInterval"], plist["RunAtLoad"], plist["ProcessType"], plist["ProgramArguments"][:2]),
            ("com.davidcasas.grade-insights", 300, False, "Background", ["/usr/sbin/taskpolicy", "-b"]),
        )

    def test_plist_pins_the_ssd_venv_python(self):
        import plistlib

        path = REPO / "deploy" / "com.davidcasas.grade-insights.plist.template"
        args = plistlib.loads(path.read_bytes())["ProgramArguments"]
        self.assertEqual(args, ["/usr/sbin/taskpolicy", "-b", "/bin/sh", "__REPO__/deploy/grade-insights-tick.sh",
                                str(runner.venv_python(runner.DEFAULT_SSD))])
        self.assertEqual(args[-1], "/Volumes/David C SSD/grade-insights/venv/bin/python")
        assert_no_loose_python(self, path.read_text())

    def test_tick_script_pins_the_same_python(self):
        text = TICK.read_text()
        self.assertEqual(text.count(f"PINNED='{runner.venv_python(runner.DEFAULT_SSD)}'"), 1)
        assert_no_loose_python(self, text)
        self.assertTrue(os.access(TICK, os.X_OK))


REPO = Path(__file__).resolve().parents[1]
TICK = REPO / "deploy" / "grade-insights-tick.sh"
PINNED = "/Volumes/David C SSD/grade-insights/venv/bin/python"


def assert_no_loose_python(case, text):
    """No bare python3, no env lookup: once the pinned SSD venv path is taken out, no interpreter is left."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "<!--", "The ", "launchd ")))
    case.assertNotIn("/usr/bin/env", code)
    case.assertNotIn("__PYTHON__", code)
    case.assertRegex(code, re.escape(PINNED))
    loose = re.findall(r"(?<![\w.])(?:\S*/)?python(?:3(?:\.\d+)?)?(?![\w.])", code.replace(PINNED, ""))
    case.assertEqual(loose, [])


class TickScriptTests(unittest.TestCase):
    """deploy/grade-insights-tick.sh, run against a copy pinned to a fake SSD with a space in its name.

    The real runner never starts: the fake interpreter only prints the arguments it got."""

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="tick test "))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.mount = self.tmp / "Fake C SSD"
        self.fake = self.mount / "grade-insights" / "venv" / "bin" / "python"
        (self.tmp / "repo" / "deploy").mkdir(parents=True)
        self.script = self.tmp / "repo" / "deploy" / "grade-insights-tick.sh"
        # The mount list is a stub: it prints mounts.txt, so a test mounts the fake SSD by writing a line.
        self.mounts = self.tmp / "mounts.txt"
        self.mounts.write_text("/dev/disk1s1 on / (apfs, local, journaled)\n")
        stub = self.tmp / "fake mount"
        stub.write_text(f"#!/bin/sh\n/bin/cat '{self.mounts}'\n")
        stub.chmod(0o755)
        text = TICK.read_text()
        self.assertEqual(text.count("MOUNT_LIST='/sbin/mount'"), 1)
        text = text.replace(PINNED, str(self.fake)).replace("MOUNT_LIST='/sbin/mount'", f"MOUNT_LIST='{stub}'")
        self.script.write_text(text)
        self.home = self.tmp / "state dir"

    def mount_ssd(self):
        with self.mounts.open("a") as f:
            f.write(f"/dev/disk4s1 on {self.mount} (apfs, local, nodev, nosuid, journaled, noowners)\n")

    def tick(self, *args):
        env = {"HOME": str(self.tmp), "GRADE_INSIGHTS_HOME": str(self.home)}
        return subprocess.run(["/bin/sh", str(self.script), *args], capture_output=True, text=True, env=env, timeout=30)

    def test_no_ssd_skips_with_one_line_a_day(self):
        first = self.tick(str(self.fake))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertRegex(first.stdout, r"^grade-insights: \d{4}-\d{2}-\d{2} skipped, the SSD is not mounted\n$")
        again = self.tick(str(self.fake))
        self.assertEqual((again.returncode, again.stdout), (0, ""))
        self.assertFalse((self.home / "state.json").exists())

    def test_mounted_ssd_execs_the_pinned_python_on_the_runner(self):
        self.fake.parent.mkdir(parents=True)
        self.fake.write_text('#!/bin/sh\nprintf "%s|" "$0" "$@"\n')
        self.fake.chmod(0o755)
        out = self.tick(str(self.fake))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, f"{self.fake}|{(self.tmp / 'repo').resolve()}/insights_runner.py|")
        self.assertEqual(self.tick().stdout, out.stdout)  # no argument: the pinned path too

    def test_any_other_interpreter_is_refused(self):
        for other in ("python3", "/usr/bin/python3", "/usr/bin/env python3", str(self.fake) + " "):
            out = self.tick(other)
            self.assertEqual((out.returncode, out.stdout), (64, ""), other)

    def test_a_symlink_a_trailing_slash_or_dotdot_is_refused(self):
        self.fake.parent.mkdir(parents=True)
        self.fake.write_text('#!/bin/sh\necho ran\n')
        self.fake.chmod(0o755)
        link = self.tmp / "python-link"
        link.symlink_to(self.fake)
        dotdot = str(self.fake.parent / ".." / "bin" / "python")
        for other in (str(link), str(self.fake) + "/", dotdot):
            out = self.tick(other)
            self.assertEqual((out.returncode, out.stdout), (64, ""), other)

    def test_a_wrong_interpreter_logs_once_a_day(self):
        first = self.tick("/usr/bin/python3")
        self.assertEqual(first.returncode, 64)
        self.assertRegex(first.stderr, r"^grade-insights: \d{4}-\d{2}-\d{2} refusing to run under anything but the SSD venv's Python\n$")
        again = self.tick("python3")
        self.assertEqual((again.returncode, again.stdout, again.stderr), (64, "", ""))
        self.assertTrue((self.home / "bad-interpreter.day").exists())

    def test_a_broken_venv_symlink_on_a_mounted_ssd_says_the_venv_python_is_missing(self):
        # Homebrew's python@3.12 is gone: the venv's python is a symlink to nothing, the SSD is there.
        self.fake.parent.mkdir(parents=True)
        self.fake.symlink_to(self.tmp / "opt" / "homebrew" / "python3.12")
        self.assertTrue(self.fake.is_symlink() and not self.fake.exists())
        self.mount_ssd()
        first = self.tick(str(self.fake))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertRegex(first.stdout, r"^grade-insights: \d{4}-\d{2}-\d{2} skipped, the venv Python is missing\n$")
        self.assertEqual(self.tick(str(self.fake)).stdout, "")
        self.assertFalse((self.home / "no-ssd.day").exists())
        # Unplugged (no mount point): back to the SSD message.
        import shutil
        shutil.rmtree(self.mount)
        self.mounts.write_text("/dev/disk1s1 on / (apfs, local, journaled)\n")
        self.assertRegex(self.tick(str(self.fake)).stdout, r"skipped, the SSD is not mounted\n$")

    def test_a_leftover_empty_folder_is_not_a_mounted_ssd(self):
        # Unplugged uncleanly: /Volumes/<name> is left behind, empty. Not mounted, so the SSD message.
        self.mount.mkdir()
        first = self.tick(str(self.fake))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertRegex(first.stdout, r"^grade-insights: \d{4}-\d{2}-\d{2} skipped, the SSD is not mounted\n$")
        self.assertEqual(self.tick(str(self.fake)).stdout, "")  # once a day
        self.assertFalse((self.home / "no-venv.day").exists())
        # A mount whose path only starts with the SSD's name ("Fake C SSD 1") doesn't count either.
        with self.mounts.open("a") as f:
            f.write(f"/dev/disk5s1 on {self.mount} 1 (apfs, local)\n")
        (self.home / "no-ssd.day").unlink()
        self.assertRegex(self.tick(str(self.fake)).stdout, r"skipped, the SSD is not mounted\n$")

    def test_mounted_without_the_venv_python_says_so(self):
        self.mount.mkdir()
        self.mount_ssd()
        first = self.tick(str(self.fake))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertRegex(first.stdout, r"^grade-insights: \d{4}-\d{2}-\d{2} skipped, the venv Python is missing\n$")
        self.assertEqual(self.tick(str(self.fake)).stdout, "")

    def test_mounted_and_present_runs(self):
        self.mount_ssd()
        self.fake.parent.mkdir(parents=True)
        self.fake.write_text('#!/bin/sh\nprintf "%s|" "$0" "$@"\n')
        self.fake.chmod(0o755)
        out = self.tick(str(self.fake))
        self.assertEqual((out.returncode, out.stderr), (0, ""))
        self.assertEqual(out.stdout, f"{self.fake}|{(self.tmp / 'repo').resolve()}/insights_runner.py|")
        self.assertFalse((self.home / "no-ssd.day").exists() or (self.home / "no-venv.day").exists())

    def test_the_real_script_uses_absolute_mount_and_grep(self):
        text = TICK.read_text()
        self.assertIn("MOUNT_LIST='/sbin/mount'", text)
        self.assertIn('"$MOUNT_LIST" 2>/dev/null | /usr/bin/grep -qF " on $MOUNT ("', text)
        self.assertNotIn('[ -d "$MOUNT" ]', text)


if __name__ == "__main__":
    unittest.main()

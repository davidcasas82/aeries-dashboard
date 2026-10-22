import json
import re
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
        self.assertNotRegex(source, r"\b(PUT|PATCH|DELETE)\b")
        self.assertEqual(re.findall(r'"POST"', source), ['"POST"', '"POST"'])
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
                if "FACTS as JSON" in system and "class story" in system:
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


if __name__ == "__main__":
    unittest.main()

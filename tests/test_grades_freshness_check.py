"""HTTP and clock are mocked. Nothing here contacts family-data or a webhook."""

import importlib.util
import io
import json
import os
import plistlib
import re
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "grades-freshness-check.py"
PLIST = REPO / "deploy" / "com.davidcasas.grades-freshness.plist"
INSIGHTS_PLIST = REPO / "deploy" / "com.davidcasas.grade-insights.plist"
PACIFIC = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 23, 17, 15, tzinfo=PACIFIC)
KEY = "wk_test_key_9f3c2a"
WEBHOOK = "https://alerts.example/hooks/wk_test_key_9f3c2a"


def load_script():
    spec = importlib.util.spec_from_file_location("grades_freshness_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp_at(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self._extra = False

    def read(self, n=None):
        if n == 1:
            return b""
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class World:
    def __init__(self, body=b"", exc=None, status=200):
        self.body = body
        self.exc = exc
        self.status = status
        self.gets = []
        self.posts = []

    def urlopen(self, request, timeout=20):
        if request.get_method() == "POST":
            self.posts.append(request)
            return FakeResponse(200, b"")
        self.gets.append(request)
        if self.exc is not None:
            raise self.exc
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "no", {}, io.BytesIO(b"secret body"))
        return FakeResponse(self.status, self.body)


def http_error(code):
    return urllib.error.HTTPError("https://family-data.example/v1/grades/freshness", code, "no", {}, io.BytesIO(b"secret body"))


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_script()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "grades-freshness.log"
        self.state = self.dir / "grades-freshness-state.json"
        self.env = self.dir / "grades-freshness.env"

    def write_env(self, mode=0o600, key=KEY, url=WEBHOOK):
        text = f"WEBHOOK_URL={url}\nWEBHOOK_KEY={key}\n"
        fd = os.open(self.env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(self.env, mode)

    def args(self, *extra):
        return ["--log", str(self.log), "--state", str(self.state), "--env-file", str(self.env), *extra]

    def run_check(self, world, now=NOW, extra=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.mod.main(self.args(*extra), now=now, opener=world.urlopen, host="mini")
        return code, stdout.getvalue(), stderr.getvalue()

    def freshness(self, updated_at, ok=True):
        body = json.dumps({"ok": ok, "updatedAt": updated_at}).encode("utf-8")
        return World(body=body)

    def assert_no_secret(self, *chunks):
        for chunk in chunks:
            self.assertNotIn(KEY, chunk)
            self.assertNotIn(WEBHOOK, chunk)
            self.assertNotIn("alerts.example", chunk)
            self.assertNotIn("Bearer ", chunk)

    def test_fractional_utc_stamp_from_the_live_shape_is_fresh(self):
        self.write_env()
        now = datetime(2026, 11, 23, 18, 24, 4, tzinfo=timezone.utc)
        world = self.freshness("2026-11-23T09:24:04.140Z")
        code, out, err = self.run_check(world, now=now)
        self.assertEqual(code, 0)
        self.assertEqual(world.posts, [])
        self.assertIn("result=fresh age_hours=9.00 alert_sent=no", out)

    def test_fresh_exits_0_and_does_not_alert(self):
        self.write_env(mode=0o644)
        world = self.freshness(stamp_at(NOW - timedelta(hours=2)))
        code, out, err = self.run_check(world)
        self.assertEqual(code, 0)
        self.assertEqual(world.posts, [])
        self.assertEqual(len(world.gets), 1)
        self.assertEqual(world.gets[0].full_url, self.mod.FRESHNESS_URL)
        self.assertEqual(world.gets[0].get_method(), "GET")
        self.assertEqual(world.gets[0].get_header("User-agent"), "grades-freshness-check/1")
        self.assertIn("result=fresh age_hours=2.00 alert_sent=no", out)
        self.assertIn("result=fresh age_hours=2.00 alert_sent=no", self.log.read_text())
        self.assertFalse(self.state.exists())
        self.assert_no_secret(out, err, self.log.read_text())

    def test_stale_over_14h_posts_one_alert(self):
        self.write_env()
        world = self.freshness(stamp_at(NOW - timedelta(hours=15)))
        code, out, err = self.run_check(world)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(len(world.posts), 1)
        post = world.posts[0]
        self.assertEqual(post.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(post.full_url, WEBHOOK)
        payload = json.loads(post.data.decode("utf-8"))
        self.assertEqual(payload["kind"], "stale")
        self.assertEqual(payload["age_hours"], 15.0)
        self.assertEqual(payload["http_status"], 200)
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["host"], "mini")
        self.assertIn("older than 14", payload["summary"])
        self.assertTrue(payload["updatedAt"].endswith("Z"))
        self.assertIn("result=stale age_hours=15.00 alert_sent=yes", self.log.read_text())
        self.assertNotIn(KEY, self.log.read_text())
        self.assertNotIn(WEBHOOK, self.log.read_text())
        self.assertNotIn(KEY, self.state.read_text())
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["alerts"]["stale"], self.mod.local_day(NOW))

    def test_exactly_14h_is_fresh_and_one_second_over_is_stale(self):
        self.write_env()
        exact = self.freshness(stamp_at(NOW - timedelta(hours=14)))
        code, out, err = self.run_check(exact)
        self.assertEqual(code, 0)
        self.assertEqual(exact.posts, [])
        self.assertIn("result=fresh age_hours=14.00 alert_sent=no", out)

        over = self.freshness(stamp_at(NOW - timedelta(hours=14, seconds=1)))
        code, out, err = self.run_check(over)
        self.assertEqual(code, 1)
        self.assertEqual(len(over.posts), 1)
        self.assertEqual(json.loads(over.posts[0].data.decode("utf-8"))["kind"], "stale")
        self.assertIn("result=stale", out)

    def test_http_errors_are_probe_broken(self):
        self.write_env()
        for code in (401, 404, 500):
            with self.subTest(code=code):
                world = World(exc=http_error(code))
                status, out, err = self.run_check(world)
                self.assertEqual(status, 2)
                self.assertEqual(len(world.posts), 1)
                payload = json.loads(world.posts[0].data.decode("utf-8"))
                self.assertEqual(payload["kind"], "probe_broken")
                self.assertEqual(payload["http_status"], code)
                self.assertEqual(payload["error"], f"HTTP {code}")
                self.assertIsNone(payload["updatedAt"])
                self.assertNotIn("secret body", json.dumps(payload))
                self.assertIn("result=probe_broken age_hours=na alert_sent=yes", out)
                self.assertEqual(world.gets[0].full_url, self.mod.FRESHNESS_URL)
                self.assertNotIn("secret body", self.log.read_text())
                self.state.unlink()
                self.log.write_text("")

    def test_timeout_and_network_error(self):
        self.write_env()
        timed = World(exc=socket.timeout("timed out"))
        code, out, err = self.run_check(timed)
        self.assertEqual(code, 2)
        payload = json.loads(timed.posts[0].data.decode("utf-8"))
        self.assertEqual(payload["error"], "timeout")
        self.assertIsNone(payload["http_status"])
        self.assertIn("result=probe_broken age_hours=na alert_sent=yes", out)
        self.state.unlink()

        down = World(exc=urllib.error.URLError(OSError("network down")))
        code, out, err = self.run_check(down)
        self.assertEqual(code, 2)
        payload = json.loads(down.posts[0].data.decode("utf-8"))
        self.assertEqual(payload["error"], "OSError")
        self.assertNotIn("network down", self.log.read_text())

    def test_bad_json_and_missing_updated_at(self):
        self.write_env()
        bad = World(body=b"not-json{")
        code, out, err = self.run_check(bad)
        self.assertEqual(code, 2)
        payload = json.loads(bad.posts[0].data.decode("utf-8"))
        self.assertEqual((payload["kind"], payload["error"], payload["http_status"]), ("probe_broken", "bad JSON", 200))
        self.state.unlink()

        missing = World(body=json.dumps({"ok": True}).encode("utf-8"))
        code, out, err = self.run_check(missing)
        self.assertEqual(code, 2)
        payload = json.loads(missing.posts[0].data.decode("utf-8"))
        self.assertEqual(payload["error"], "missing or unparseable updatedAt")
        self.assertIsNone(payload["updatedAt"])
        self.assertIsNone(payload["age_hours"])

    def test_ok_false_is_probe_broken_even_when_the_stamp_is_recent(self):
        self.write_env()
        world = self.freshness(stamp_at(NOW - timedelta(minutes=5)), ok=False)
        code, out, err = self.run_check(world)
        self.assertEqual(code, 2)
        payload = json.loads(world.posts[0].data.decode("utf-8"))
        self.assertEqual(payload["kind"], "probe_broken")
        self.assertEqual(payload["error"], "ok is not true")
        self.assertIsNotNone(payload["updatedAt"])

    def test_dedupe_within_a_day_and_a_new_day_realerts(self):
        self.write_env()
        first = self.freshness(stamp_at(NOW - timedelta(hours=15)))
        code, out, err = self.run_check(first, now=NOW)
        self.assertEqual(code, 1)
        self.assertEqual(len(first.posts), 1)

        second = self.freshness(stamp_at(NOW - timedelta(hours=16)))
        code, out, err = self.run_check(second, now=NOW + timedelta(hours=1))
        self.assertEqual(code, 1)
        self.assertEqual(second.posts, [])
        self.assertIn("alert_sent=deduped", out)

        later = NOW + timedelta(days=1)
        third = self.freshness(stamp_at(later - timedelta(hours=15)))
        code, out, err = self.run_check(third, now=later)
        self.assertEqual(code, 1)
        self.assertEqual(len(third.posts), 1)
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["alerts"]["stale"], self.mod.local_day(later))
        self.assertNotEqual(saved["alerts"]["stale"], self.mod.local_day(NOW))

    def test_a_different_kind_still_alerts_the_same_day(self):
        self.write_env()
        stale = self.freshness(stamp_at(NOW - timedelta(hours=15)))
        self.run_check(stale)
        broken = World(exc=http_error(401))
        code, out, err = self.run_check(broken)
        self.assertEqual(code, 2)
        self.assertEqual(len(broken.posts), 1)
        self.assertEqual(json.loads(broken.posts[0].data.decode("utf-8"))["kind"], "probe_broken")
        saved = json.loads(self.state.read_text())
        self.assertEqual(set(saved["alerts"]), {"stale", "probe_broken"})

    def test_key_never_appears_in_log_output(self):
        self.write_env()
        world = World(exc=http_error(401))
        code, out, err = self.run_check(world)
        text = "\n".join([out, err, self.log.read_text(), self.state.read_text()])
        self.assert_no_secret(text)
        self.assertEqual(world.posts[0].get_header("Authorization"), "Bearer " + KEY)

        self.state.unlink()

        def explode(request, timeout=20):
            if request.get_method() == "POST":
                raise urllib.error.HTTPError(WEBHOOK, 500, "no", {}, io.BytesIO(KEY.encode("utf-8")))
            raise http_error(401)

        refused = World()
        refused.urlopen = explode
        code, out, err = self.run_check(refused)
        self.assert_no_secret(out, err, self.log.read_text())
        self.assertIn("alert not sent: HTTP 500", err)
        self.assertIn("alert_sent=no", out)
        self.assertFalse(self.state.exists())

    def test_loose_env_file_is_not_read(self):
        self.write_env(mode=0o644)
        world = self.freshness(stamp_at(NOW - timedelta(hours=15)))
        code, out, err = self.run_check(world)
        self.assertEqual(code, 1)
        self.assertEqual(world.posts, [])
        self.assertIn("alert not sent: webhook env file is not mode 0600", err)
        self.assertIn("alert_sent=no", out)
        self.assert_no_secret(out, err, self.log.read_text())
        self.assertFalse(self.state.exists())

    def test_dry_run_prints_the_payload_and_does_not_post_or_log(self):
        self.write_env()
        world = self.freshness(stamp_at(NOW - timedelta(hours=15)))
        code, out, err = self.run_check(world, extra=("--dry-run",))
        self.assertEqual(code, 1)
        self.assertEqual(world.posts, [])
        self.assertIn("dry-run: would POST", out)
        self.assertIn('"kind": "stale"', out)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.state.exists())
        self.assert_no_secret(out, err)

        fresh = self.freshness(stamp_at(NOW - timedelta(hours=1)))
        code, out, err = self.run_check(fresh, extra=("--dry-run",))
        self.assertEqual(code, 0)
        self.assertIn("dry-run: no alert", out)
        self.assertEqual(fresh.posts, [])

    def test_test_alert_is_labeled_and_can_be_dry(self):
        self.write_env()
        world = World()
        code, out, err = self.run_check(world, extra=("--test-alert", "--dry-run"))
        self.assertEqual(code, 0)
        self.assertEqual(world.gets, [])
        self.assertEqual(world.posts, [])
        self.assertIn("TEST grade freshness check", out)
        payload = json.loads(out[out.index("{") : out.rindex("}") + 1])
        self.assertEqual(payload["kind"], "test")
        self.assertTrue(payload["summary"].startswith("TEST"))

        code, out, err = self.run_check(world, extra=("--test-alert",))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(world.gets, [])
        self.assertEqual(len(world.posts), 1)
        sent = json.loads(world.posts[0].data.decode("utf-8"))
        self.assertEqual(sent["kind"], "test")
        self.assertTrue(sent["summary"].startswith("TEST"))
        self.assertIn("result=test age_hours=na alert_sent=yes", self.log.read_text())
        self.assertFalse(self.state.exists())
        self.assert_no_secret(out, err, self.log.read_text())

    def test_http_webhook_url_is_rejected(self):
        self.write_env(url="http://alerts.example/hook")
        world = self.freshness(stamp_at(NOW - timedelta(hours=15)))
        code, out, err = self.run_check(world)
        self.assertEqual(code, 1)
        self.assertEqual(world.posts, [])
        self.assertIn("alert not sent: webhook url must be https", err)
        self.assertIn("alert_sent=no", out)
        self.assertNotIn("http_status=", out)
        self.assert_no_secret(out, err, self.log.read_text())
        self.assertNotIn("http://alerts.example/hook", out + err + self.log.read_text())
        self.assertFalse(self.state.exists())

    def test_redirect_is_not_followed(self):
        sink_hits = []
        redirect_hits = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or "0")
                if length:
                    self.rfile.read(length)
                self.server.hits.append(self.headers.get("Authorization"))
                self.send_response(self.server.reply_status)
                if self.server.location:
                    self.send_header("Location", self.server.location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        sink.hits = sink_hits
        sink.reply_status = 200
        sink.location = None
        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        redirect.hits = redirect_hits
        redirect.reply_status = 302
        redirect.location = "http://127.0.0.1:%d/landed" % sink.server_address[1]
        threads = []
        for server in (sink, redirect):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        self.addCleanup(sink.shutdown)
        self.addCleanup(redirect.shutdown)
        self.addCleanup(sink.server_close)
        self.addCleanup(redirect.server_close)

        redirect_url = "http://127.0.0.1:%d/hook" % redirect.server_address[1]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.mod._post_json(redirect_url, {"kind": "stale", "summary": "old"}, KEY)
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(redirect_hits, ["Bearer " + KEY])
        self.assertEqual(sink_hits, [])

        self.write_env()
        world = self.freshness(stamp_at(NOW - timedelta(hours=15)))

        def opener(request, timeout=20):
            if request.get_method() == "GET":
                return world.urlopen(request, timeout)
            local = urllib.request.Request(
                redirect_url,
                data=request.data,
                method="POST",
                headers={"Authorization": request.get_header("Authorization"), "Content-Type": "application/json"},
            )
            return self.mod._urlopen_no_redirect(local, timeout)

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.mod.main(self.args(), now=NOW, opener=opener, host="mini")
        out, err = stdout.getvalue(), stderr.getvalue()
        self.assertEqual(code, 1)
        self.assertEqual(sink_hits, [])
        self.assertEqual(redirect_hits, ["Bearer " + KEY, "Bearer " + KEY])
        self.assertIn("alert not sent: HTTP 302", err)
        self.assertIn("alert_sent=no http_status=302", out)
        self.assertIn("alert_sent=no http_status=302", self.log.read_text())
        self.assert_no_secret(out, err, self.log.read_text())
        self.assertFalse(self.state.exists())

    def test_script_only_mentions_the_freshness_path(self):
        text = SCRIPT.read_text()
        self.assertEqual(set(re.findall(r"/v1/[A-Za-z0-9/_-]+", text)), {"/v1/grades/freshness"})
        self.assertNotIn("FAMILY_PIN", text)
        self.assertNotIn("grades_data", text)

    def test_default_paths_live_under_application_support(self):
        home = Path("/Users/example")
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("GRADES_FRESHNESS_ENV", "GRADES_FRESHNESS_LOG", "GRADES_FRESHNESS_STATE"):
                os.environ.pop(name, None)
            with mock.patch.object(self.mod.Path, "home", return_value=home):
                env = self.mod.default_env_file()
                log = self.mod.default_log_file()
                state = self.mod.default_state_file(log)
        root = home / "Library" / "Application Support" / "aeries-dashboard"
        self.assertEqual(env, root / "grades-freshness.env")
        self.assertEqual(log, root / "grades-freshness.log")
        self.assertEqual(state, log.with_name("grades-freshness-state.json"))
        self.assertEqual(state.parent, log.parent)


class PlistTests(unittest.TestCase):
    def load(self):
        return plistlib.loads(PLIST.read_bytes())

    def test_weekdays_at_5_15_pm_local(self):
        plist = self.load()
        insights = plistlib.loads(INSIGHTS_PLIST.read_bytes())
        slots = plist["StartCalendarInterval"]
        self.assertEqual(plist["Label"], "com.davidcasas.grades-freshness")
        self.assertFalse(plist["RunAtLoad"])
        self.assertNotIn("StartInterval", plist)
        self.assertEqual(plist["WorkingDirectory"], insights["WorkingDirectory"])
        self.assertEqual({slot["Weekday"] for slot in slots}, {1, 2, 3, 4, 5})
        self.assertEqual(len(slots), 5)
        for slot in slots:
            self.assertEqual((slot["Hour"], slot["Minute"]), (17, 15))
        args = plist["ProgramArguments"]
        self.assertEqual(args[0], "/usr/bin/python3")
        self.assertEqual(args[1], plist["WorkingDirectory"] + "/scripts/grades-freshness-check.py")
        self.assertTrue(SCRIPT.is_file())
        self.assertIn("--env-file", args)
        self.assertTrue(args[args.index("--log") + 1].endswith("/grades-freshness.log"))
        self.assertTrue(args[args.index("--state") + 1].endswith("/grades-freshness-state.json"))
        self.assertEqual(
            Path(args[args.index("--state") + 1]).parent,
            Path(args[args.index("--log") + 1]).parent,
        )
        mode = stat.S_IMODE(SCRIPT.stat().st_mode)
        self.assertTrue(mode & 0o111)

    def test_label_program_and_weekday_interval(self):
        plist = self.load()
        args = plist["ProgramArguments"]
        slots = plist["StartCalendarInterval"]
        self.assertEqual(plist["Label"], "com.davidcasas.grades-freshness")
        self.assertEqual(args[0], "/usr/bin/python3")
        self.assertTrue(str(args[1]).endswith("/scripts/grades-freshness-check.py"))
        self.assertEqual(
            sorted((slot["Weekday"], slot["Hour"], slot["Minute"]) for slot in slots),
            [(day, 17, 15) for day in range(1, 6)],
        )

    @unittest.skipUnless(Path("/usr/bin/plutil").is_file(), "plutil is macOS only")
    def test_plutil_lint(self):
        done = subprocess.run(
            ["/usr/bin/plutil", "-lint", str(PLIST)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()

import re
import sys
import tempfile
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
        self.assertEqual(re.findall(r"urlopen\(", source), ["urlopen(", "urlopen("])

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

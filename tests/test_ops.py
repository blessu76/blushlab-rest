"""ops/slot_gate.py: slot draw, one post a day, queue order, git safety, alerts; queue stays out of the build; datePublished carries the time."""
import contextlib
import datetime as dt
import io
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import REPO, build, jsonld, make_site  # noqa: E402

sys.path.insert(0, str(REPO / "ops"))
import slot_gate as sg  # noqa: E402

D = dt.date

# every test records macOS notices here instead of calling osascript
NOTES = []
sg.notify = lambda title, msg: NOTES.append((title, msg))


def fake_repo(n_queue=3):
    """a temp 'repo' holding only content/journal + content/queue copied from the real tree (12 languages each)."""
    root = Path(tempfile.mkdtemp(prefix="blushlab-ops-"))
    qs = sorted((REPO / "content" / "queue").iterdir())[:n_queue]
    for d in qs:
        shutil.copytree(d, root / "content" / "queue" / d.name)
    (root / "content" / "journal").mkdir(parents=True)
    return root


class SlotDrawTest(unittest.TestCase):
    def test_slot_in_window_and_quarter_hours(self):
        rng = random.Random(1)
        for _ in range(500):
            t = sg.draw_time(rng)
            h, m = map(int, t.split(":"))
            self.assertTrue(dt.time(12, 0) <= dt.time(h, m) <= dt.time(18, 45), t)
            self.assertEqual(m % 15, 0, t)

    def test_slot_differs_from_yesterday(self):
        for seed in range(300):
            rng = random.Random(seed)
            st = {"date": "2026-11-19", "time": "%02d:%02d" % (12 + seed % 7, 15 * (seed % 4)), "published": True}
            new, rolled = sg.roll(st, D(2026, 11, 20), rng)
            self.assertTrue(rolled)
            self.assertNotEqual(sg.hour_of(new["time"]), sg.hour_of(st["time"]))
            self.assertEqual((new["prev_date"], new["prev_time"]), ("2026-11-19", st["time"]))
            self.assertFalse(new["published"])

    def test_same_day_is_not_redrawn(self):
        st = {"date": "2026-11-20", "time": "13:15", "published": False}
        new, rolled = sg.roll(st, D(2026, 11, 20), random.Random(0))
        self.assertFalse(rolled)
        self.assertEqual(new, st)

    def test_decide(self):
        st = {"date": "2026-11-20", "time": "13:15", "published": False}
        kst = sg.KST
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 13, 0, tzinfo=kst), st)[0], "wait")
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 13, 15, tzinfo=kst), st)[0], "due")
        st["published"] = True
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 18, 45, tzinfo=kst), st)[0], "published")

    def test_next_slot(self):
        kst = sg.KST
        now = dt.datetime(2026, 11, 20, 9, 0, tzinfo=kst)
        w, _ = sg.next_slot(now, {"date": "2026-11-20", "time": "14:30", "published": False})
        self.assertEqual(w, dt.datetime(2026, 11, 20, 14, 30, tzinfo=kst))
        w, _ = sg.next_slot(now, {"date": "2026-11-20", "time": "14:30", "published": True})
        self.assertEqual(w.date(), D(2026, 11, 21))

    def test_window_end_is_judged_to_the_minute(self):
        """item 1: 18:45:30 is still the 18:45 slot; seconds never close the window early."""
        kst = sg.KST
        st = {"date": "2026-11-20", "time": "18:45", "published": False}
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 18, 45, 0, tzinfo=kst), st)[0], "due")
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 18, 45, 30, tzinfo=kst), st)[0], "due")
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 18, 45, 59, tzinfo=kst), st)[0], "due")
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 18, 46, 0, tzinfo=kst), st)[0], "expired")
        w, which = sg.next_slot(dt.datetime(2026, 11, 20, 18, 45, 30, tzinfo=kst), st)
        self.assertEqual((w, which), (dt.datetime(2026, 11, 20, 18, 45, tzinfo=kst), "today"))
        st2 = {"date": "2026-11-20", "time": "13:15", "published": False}
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 13, 14, 59, tzinfo=kst), st2)[0], "wait")
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 13, 15, 30, tzinfo=kst), st2)[0], "due")


class OnePostADayTest(unittest.TestCase):
    def gate(self, repo, base, hour, minute=0, day=20):
        calls = []

        def pub(r, d, hm, **kw):
            calls.append((d, hm))
            return sg.publish_one(r, d, hm, pull=False, **kw)

        # no git/build in unit tests: publish_one's subprocess steps are replaced
        sg.run, self._old = (lambda *a, **k: ""), sg.run
        self.addCleanup(lambda: setattr(sg, "run", self._old))
        g = sg.Gate(Path(base) / "slot.json", repo, base, publish_fn=pub, rng=random.Random(7), log=lambda m: None,
                    now_fn=lambda: dt.datetime(2026, 11, day, hour, minute, tzinfo=sg.KST))
        return g, calls

    def test_one_post_per_day_and_time_in_published(self):
        repo, base = fake_repo(3), Path(tempfile.mkdtemp())
        g, calls = self.gate(repo, base, 12)
        g.tick(roll_only=True)
        slot = sg.load_state(base / "slot.json")["time"]
        h, m = map(int, slot.split(":"))
        g.now_fn = lambda: dt.datetime(2026, 11, 20, h, m, tzinfo=sg.KST)
        self.assertEqual(g.tick()[0], "published-now")
        text = (repo / "content" / "journal" / "two-modes-one-game" / "en.md").read_text(encoding="utf-8")
        self.assertIn("published: 2026-11-20T%s+09:00" % slot, text)
        self.assertEqual(len(sg.queue_items(repo)), 2)
        for _ in range(3):  # later ticks the same day publish nothing
            g.now_fn = lambda: dt.datetime(2026, 11, 20, 18, 45, tzinfo=sg.KST)
            self.assertEqual(g.tick()[0], "published")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(list((repo / "content" / "journal").iterdir())), 1)
        # next day: a new slot, a different hour, and exactly one more post
        g.now_fn = lambda: dt.datetime(2026, 11, 21, 18, 45, tzinfo=sg.KST)
        self.assertEqual(g.tick()[0], "published-now")
        st = sg.load_state(base / "slot.json")
        self.assertNotEqual(sg.hour_of(st["time"]), sg.hour_of(slot))
        self.assertEqual(len(list((repo / "content" / "journal").iterdir())), 2)

    def test_not_before_slot(self):
        repo, base = fake_repo(3), Path(tempfile.mkdtemp())
        g, calls = self.gate(repo, base, 11, 59)
        self.assertEqual(g.tick()[0], "wait")
        self.assertEqual(calls, [])

    def test_empty_queue_logs_and_stops(self):
        repo, base = fake_repo(0), Path(tempfile.mkdtemp())
        g, calls = self.gate(repo, base, 18, 45)
        g.tick(roll_only=True)
        self.assertEqual(g.tick()[0], "empty")
        self.assertFalse((base / "FAILED").exists())

    def test_incomplete_queue_entry_is_not_published(self):
        repo, base = fake_repo(1), Path(tempfile.mkdtemp())
        next((repo / "content" / "queue").iterdir()).joinpath("tr.md").unlink()
        g, _ = self.gate(repo, base, 18, 45)
        g.tick(roll_only=True)
        self.assertEqual(g.tick()[0], "failed")  # item 7: SoftFail, retried next tick
        self.assertEqual(list((repo / "content" / "journal").iterdir()), [])
        self.assertFalse((base / "FAILED").exists())

    def test_incomplete_head_is_not_skipped(self):
        """item 7: an incomplete head blocks the queue; the complete entry behind it is not published out of order."""
        repo, base = fake_repo(2), Path(tempfile.mkdtemp())
        first = sorted((repo / "content" / "queue").iterdir())[0]
        first.joinpath("tr.md").unlink()
        g, _ = self.gate(repo, base, 18, 45)
        g.tick(roll_only=True)
        self.assertEqual(g.tick()[0], "failed")
        self.assertEqual(list((repo / "content" / "journal").iterdir()), [])
        self.assertEqual(len(sg.queue_items(repo)), 2)
        self.assertFalse(sg.load_state(base / "slot.json")["published"])

    def test_stamp_refuses_double_publish(self):
        with self.assertRaises(ValueError):
            sg.stamp_published("---\ntitle: x\npublished: 2026-11-19T10:00+09:00\n---\nbody", "2026-11-20T12:00+09:00")

    def test_stock_warning(self):
        self.assertIsNone(sg.stock_warning(3))
        self.assertIn("2 posts", sg.stock_warning(2))


class QueueOutOfBuildTest(unittest.TestCase):
    def test_queue_not_in_sitemap_or_pages(self):
        root = make_site(queue=("q-one", "q-two", "q-three"))
        build(root)
        sm = (root / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("alpha-post", sm)
        for slug in ("q-one", "q-two", "q-three"):
            self.assertNotIn(slug, sm)
            self.assertFalse(any(root.glob("**/journal/%s/index.html" % slug)))
            for f in ("journal/index.html", "journal/feed.xml", "llms.txt"):
                self.assertNotIn(slug, (root / f).read_text(encoding="utf-8"))

    def test_real_queue_is_complete_and_unbuilt(self):
        items = sg.queue_items(REPO)
        self.assertGreaterEqual(len(items), 3)
        sm = (REPO / "sitemap.xml").read_text(encoding="utf-8")
        for d, slug, miss in items:
            self.assertEqual(miss, [], d.name)
            self.assertNotIn("/%s/" % slug, sm)


class DatePublishedTimeTest(unittest.TestCase):
    def test_json_ld_date_has_time(self):
        root = make_site()
        build(root)
        html = (root / "journal" / "alpha-post" / "index.html").read_text(encoding="utf-8")
        self.assertIn('"datePublished":"2026-11-01T18:00:00+09:00"', html)
        self.assertTrue(jsonld(html))

    def test_real_post_today_has_time_in_post_list_feed_sitemap(self):
        slug = "naming-blushlab"
        src = (REPO / "content" / "journal" / slug / "en.md").read_text(encoding="utf-8")
        self.assertRegex(src, r"(?m)^published: 2026-11-19T\d\d:\d\d\+09:00$")
        post = (REPO / "journal" / slug / "index.html").read_text(encoding="utf-8")
        self.assertRegex(post, r'"datePublished":\s*"2026-11-19T\d\d:\d\d:00\+09:00"')
        self.assertIn("/journal/%s/" % slug, (REPO / "journal" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("/journal/%s/" % slug, (REPO / "journal" / "feed.xml").read_text(encoding="utf-8"))
        self.assertIn("/journal/%s/" % slug, (REPO / "sitemap.xml").read_text(encoding="utf-8"))


class SlotWindowTest(unittest.TestCase):
    def test_missed_slot_published_only_inside_window(self):
        kst = sg.KST
        st = {"date": "2026-11-20", "time": "16:30", "published": False}
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 18, 45, tzinfo=kst), st)[0], "due")  # 18:45 itself is still inside
        self.assertEqual(sg.decide(dt.datetime(2026, 11, 20, 19, 0, tzinfo=kst), st)[0], "expired")  # Mac woke after the window
        w, _ = sg.next_slot(dt.datetime(2026, 11, 20, 19, 0, tzinfo=kst), st)
        self.assertEqual(w.date(), D(2026, 11, 21))

    def test_expired_tick_publishes_nothing_and_alerts_once(self):
        """item 4: a missed window notifies once for that day, not on every later tick."""
        NOTES.clear()
        repo, base = fake_repo(3), Path(tempfile.mkdtemp())
        calls = []
        g = sg.Gate(base / "slot.json", repo, base, publish_fn=lambda *a, **k: calls.append(a) or ("published", "x"),
                    rng=random.Random(7), log=lambda m: None, now_fn=lambda: dt.datetime(2026, 11, 20, 12, 0, tzinfo=sg.KST))
        g.tick(roll_only=True)
        sg.save_state(base / "slot.json", {"date": "2026-11-20", "time": "12:00", "published": False})
        g.now_fn = lambda: dt.datetime(2026, 11, 20, 19, 10, tzinfo=sg.KST)
        # fake repo is not a git clone: origin is reachable (no-op fetch) and has no post today
        with mock.patch.object(sg, "fetch", lambda *a, **k: None), mock.patch.object(sg, "origin_post_dated", lambda *a, **k: None):
            self.assertEqual(g.tick()[0], "expired")
            self.assertEqual(g.tick()[0], "expired")
        self.assertEqual(calls, [])
        self.assertEqual(len([n for n in NOTES if "놓침" in n[0]]), 1)

    def test_rollover_alerts_missed_previous_day_once(self):
        """item 4: the day after an unpublished day is reported at that day's rollover tick (launchd's last tick is 18:45)."""
        NOTES.clear()
        repo, base = fake_repo(3), Path(tempfile.mkdtemp())
        sg.save_state(base / "slot.json", {"date": "2026-11-19", "time": "13:00", "published": False})
        g = sg.Gate(base / "slot.json", repo, base, rng=random.Random(7), log=lambda m: None,
                    now_fn=lambda: dt.datetime(2026, 11, 20, 12, 0, tzinfo=sg.KST))
        self.assertEqual(g.tick()[0], "wait")
        self.assertEqual(g.tick()[0], "wait")
        missed = [n for n in NOTES if "놓침" in n[0]]
        self.assertEqual(len(missed), 1)
        self.assertIn("2026-11-19", missed[0][1])


class RepoGuardTest(unittest.TestCase):
    def test_only_dedicated_clone_without_explicit_repo(self):
        self.assertIsNone(sg.guard_repo(sg.DEDICATED_REPO, explicit=False))
        # "some other checkout" must be a path that is not the dedicated clone, wherever the suite runs
        # (this checkout itself is the dedicated clone when run from ~/.ipe/blushlab-publish/repo)
        other = Path(tempfile.gettempdir()) / "blushlab-other-checkout"  # never created; guard only compares paths
        self.assertNotEqual(other.resolve(), sg.DEDICATED_REPO.resolve())
        self.assertIsNotNone(sg.guard_repo(other, explicit=False))
        self.assertIsNone(sg.guard_repo(REPO, explicit=True))  # --repo given (tests)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout


class GitPublishTest(unittest.TestCase):
    """real git: a bare origin, a work clone, and stub build/check scripts whose failures are switched by flag files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="blushlab-gitops-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.flags = self.tmp / "flags"
        self.flags.mkdir()
        NOTES.clear()
        self.notes = NOTES
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        self.base = self.tmp / "base"
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\ntest -f '%s' && exit 1\nexit 0\n" % (self.flags / "reject_push"))
        hook.chmod(0o755)
        _git(self.tmp, "init", "-q", "-b", "main", str(self.work))
        _git(self.work, "config", "user.name", "test")
        _git(self.work, "config", "user.email", "test@example.invalid")
        for d in sorted((REPO / "content" / "queue").iterdir())[:3]:
            shutil.copytree(str(d), str(self.work / "content" / "queue" / d.name))
        (self.work / "content" / "journal").mkdir(parents=True)
        (self.work / "content" / "journal" / ".gitkeep").write_text("")
        (self.work / "scripts").mkdir()
        (self.work / "scripts" / "build_site.py").write_text(
            "import os, sys\nsys.exit(1 if os.path.exists('%s') else 0)\n" % (self.flags / "build_fail"))
        (self.work / "scripts" / "check.sh").write_text("#!/bin/bash\ntest -f '%s' && exit 1\nexit 0\n" % (self.flags / "check_fail"))
        pc = self.work / ".git" / "hooks" / "pre-commit"
        pc.write_text("#!/bin/sh\ntest -f '%s' && exit 1\nexit 0\n" % (self.flags / "commit_fail"))
        pc.chmod(0o755)
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "init")
        _git(self.work, "remote", "add", "origin", str(self.origin))
        _git(self.work, "push", "-q", "-u", "origin", "main")
        # restore() only ever resets the dedicated clone: this fixture clone stands in for it. Tests that need a
        # non-dedicated clone override DEDICATED_REPO themselves.
        p = mock.patch.object(sg, "DEDICATED_REPO", self.work)
        p.start()
        self.addCleanup(p.stop)

    def remote_post(self, day, slug="remote-post"):
        """push a journal post dated `day` to origin/main from another clone (this clone's origin ref stays stale)."""
        other = self.tmp / ("other-" + slug)
        _git(self.tmp, "clone", "-q", str(self.origin), str(other))
        _git(other, "config", "user.name", "test")
        _git(other, "config", "user.email", "test@example.invalid")
        post = other / "content" / "journal" / slug
        post.mkdir(parents=True)
        (post / "en.md").write_text("---\ntitle: x\npublished: %sT13:15+09:00\n---\nbody\n" % day, encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "remote post")
        _git(other, "push", "-q", "origin", "main")

    def flag(self, name):
        (self.flags / name).write_text("1")

    def status(self):
        return _git(self.work, "status", "--porcelain")

    def assert_restored(self):
        self.assertEqual(self.status(), "")
        self.assertEqual(_git(self.work, "diff", "--cached", "--name-only"), "")
        self.assertEqual(len(sg.queue_items(self.work)), 3)  # the queue entry is back
        self.assertEqual(sorted(p.name for p in (self.work / "content" / "journal").iterdir()), [".gitkeep"])

    def gate(self, hour, minute=0, **kw):
        return sg.Gate(self.base / "slot.json", self.work, self.base, remote="origin", rng=random.Random(3),
                       log=lambda m: None, now_fn=lambda: dt.datetime(2026, 11, 20, hour, minute, tzinfo=sg.KST),
                       allow_reset=True, **kw)

    def test_commit_failure_restores_and_is_hard_fail(self):
        self.flag("commit_fail")
        with self.assertRaises(sg.HardFail):
            sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True)
        self.assert_restored()
        self.assertEqual(_git(self.work, "log", "--oneline", "-1").split(" ", 1)[1].strip(), "init")

    def test_commit_failure_without_allow_reset_leaves_files(self):
        """the clone is restored only when reset is allowed; otherwise the leftovers stay for a human to see."""
        self.flag("commit_fail")
        with self.assertRaises(sg.HardFail):
            sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=False)
        self.assertNotEqual(self.status(), "")
        (self.flags / "commit_fail").unlink()
        _git(self.work, "reset", "-q", "--hard", "HEAD")
        _git(self.work, "clean", "-fdq")

    def test_push_failure_is_hard_fail_with_failed_file_and_notice(self):
        self.flag("reject_push")
        g = self.gate(18, 45)
        self.assertEqual(g.tick()[0], "failed")
        self.assertTrue((self.base / "FAILED").is_file())
        self.assertEqual(len(self.notes), 1)
        self.assertFalse(sg.load_state(self.base / "slot.json")["published"])
        self.assertTrue(_git(self.work, "log", "--oneline", "-1").split(" ", 1)[1].startswith("journal:"))  # commit stays local
        # next day: FAILED still there -> blocked, notified again, nothing published
        (self.base / "slot.json").unlink()
        g.now_fn = lambda: dt.datetime(2026, 11, 21, 18, 45, tzinfo=sg.KST)
        self.assertEqual(g.tick()[0], "blocked")
        self.assertEqual(len(self.notes), 2)

    def test_failed_notice_is_once_per_day(self):
        """item 6: the same day's later ticks do not notify again."""
        self.flag("reject_push")
        g = self.gate(18, 45)
        g.tick()
        self.assertEqual(g.tick()[0], "blocked")
        self.assertEqual(g.tick()[0], "blocked")
        self.assertEqual(len(self.notes), 1)

    def test_failed_removal_does_not_unblock(self):
        """item 2: removing FAILED while the local commit is still not on origin/main stops again at the same check."""
        self.flag("reject_push")
        first_slug = sg.queue_items(self.work)[0][1]
        g = self.gate(18, 45)
        self.assertEqual(g.tick()[0], "failed")
        sha = _git(self.work, "rev-parse", "--short", "HEAD").strip()
        (self.base / "FAILED").unlink()
        (self.base / "slot.json").unlink()  # same day, re-drawn: still due
        g.now_fn = lambda: dt.datetime(2026, 11, 20, 18, 45, tzinfo=sg.KST)
        self.assertEqual(g.tick()[0], "failed")
        self.assertTrue((self.base / "FAILED").is_file())
        self.assertIn(sha, (self.base / "FAILED").read_text(encoding="utf-8"))
        self.assertEqual(len(sg.queue_items(self.work)), 2)  # only the first run's head was taken (it sits in the local commit); no second post
        self.assertEqual(sorted(p.name for p in (self.work / "content" / "journal").iterdir()), sorted([".gitkeep", first_slug]))

    def test_local_only_commit_blocks_publishing(self):
        """item 2: a local commit origin/main does not have stops publishing before anything is written."""
        dest = self.work / "content" / "journal" / "local-only"
        dest.mkdir()
        (dest / "en.md").write_text("---\ntitle: x\npublished: 2026-11-20T10:00+09:00\n---\nbody\n", encoding="utf-8")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "local draft")
        with self.assertRaises(sg.HardFail) as cm:
            sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True)
        self.assertIn("local commit", str(cm.exception))
        self.assertEqual(len(sg.queue_items(self.work)), 3)

    def test_build_and_check_failure_restore_then_soft_fail(self):
        for name in ("build_fail", "check_fail"):
            self.flag(name)
            with self.assertRaises(sg.SoftFail):
                sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True)
            self.assert_restored()
            (self.flags / name).unlink()
        g = self.gate(18, 45)
        self.flag("check_fail")
        self.assertEqual(g.tick()[0], "failed")
        self.assertFalse((self.base / "FAILED").exists())  # retry on the next tick, no human needed
        self.assertFalse(sg.load_state(self.base / "slot.json")["published"])
        # the 18:45:00 tick is the last one: its miss is reported (MISSED), but it is not a FAILED stop
        self.assertEqual([t for t, _ in self.notes], ["블러쉬랩 발행 놓침"])

    def test_pull_failure_restores_and_is_soft_fail(self):
        """item 5: the pull is inside the restore step; a failed pull aborts the rebase and retries next tick."""
        real = sg.run

        def boom(cmd, cwd, what):
            if cmd[:2] == ["git", "pull"]:
                raise sg.SoftFail("git pull --rebase failed (1): boom")
            return real(cmd, cwd, what)

        with mock.patch.object(sg, "run", side_effect=boom):
            with self.assertRaises(sg.SoftFail):
                sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True)
        self.assert_restored()
        self.assertFalse((self.work / ".git" / "rebase-merge").exists())
        self.assertFalse((self.work / ".git" / "rebase-apply").exists())

    def test_pull_moves_queue_head(self):
        """item 5: the queue is read again after the pull, so a head removed upstream is not published."""
        items = sg.queue_items(self.work)
        first, second = items[0], items[1]
        other = self.tmp / "other"
        _git(self.tmp, "clone", "-q", str(self.origin), str(other))
        _git(other, "config", "user.name", "test")
        _git(other, "config", "user.email", "test@example.invalid")
        shutil.rmtree(str(other / "content" / "queue" / first[0].name))
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "drop head")
        _git(other, "push", "-q", "origin", "main")
        status, slug = sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True)
        self.assertEqual((status, slug), ("published", second[1]))
        self.assertTrue((self.work / "content" / "journal" / second[1]).is_dir())
        self.assertFalse((self.work / "content" / "journal" / first[1]).exists())

    def test_unexpected_exception_is_logged_restored_and_retried(self):
        """item 3: an unexpected error logs its traceback, restores the clone, and is a SoftFail (no FAILED file)."""
        logs = []

        def bad(repo, day, hm, **kw):
            (Path(repo) / "content" / "journal" / "junk.txt").write_text("x")
            raise RuntimeError("kaboom")

        g = sg.Gate(self.base / "slot.json", self.work, self.base, publish_fn=bad, remote="origin", allow_reset=True,
                    rng=random.Random(3), log=logs.append,
                    now_fn=lambda: dt.datetime(2026, 11, 20, 18, 45, tzinfo=sg.KST))
        self.assertEqual(g.tick()[0], "failed")
        joined = "\n".join(logs)
        self.assertIn("unexpected RuntimeError: kaboom", joined)
        self.assertIn("Traceback", joined)
        self.assert_restored()
        self.assertFalse((self.base / "FAILED").exists())
        self.assertFalse(sg.load_state(self.base / "slot.json")["published"])

    def test_missing_clone_logs_notifies_and_exits_without_crash(self):
        """item 3: a missing dedicated clone is logged and notified; main returns 1 instead of raising."""
        NOTES.clear()
        gone = self.tmp / "no-clone"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sg.main(["--repo", str(gone), "--base", str(self.base), "--now", "2026-11-20T18:45:00+09:00"])
        self.assertEqual(rc, 1)
        self.assertIn("전용 클론 없음", buf.getvalue())
        self.assertTrue(any("전용 클론이 없습니다" in m for _, m in NOTES))

    def test_already_is_judged_on_origin_main(self):
        self.assertEqual(sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin")[0], "published")
        # this clone falls back behind origin: today's post exists on origin/main only
        _git(self.work, "reset", "-q", "--hard", "HEAD~1")
        status, slug = sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin")
        self.assertEqual(status, "already")
        self.assertEqual(slug, "two-modes-one-game")
        self.assertEqual(self.status(), "")  # nothing written, nothing staged
        self.assertEqual(sorted(p.name for p in (self.work / "content" / "journal").iterdir()), [".gitkeep"])
        self.assertEqual(len(sg.queue_items(self.work)), 3)  # this clone's queue untouched

    def test_dirty_clone_is_restored_then_published_when_reset_allowed(self):
        """review 1: a tree left dirty by an interrupted run is restored (allow_reset, dedicated clone only) and the run goes on; the restore is logged."""
        (self.work / "stray.txt").write_text("left over")
        logs = []
        with mock.patch.object(sg, "DEDICATED_REPO", self.work):
            status, slug = sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True, log=logs.append)
        self.assertEqual(status, "published")
        self.assertTrue(any("dirty" in m and "restored" in m for m in logs))
        self.assertFalse((self.work / "stray.txt").exists())
        self.assertTrue(_git(self.work, "log", "--oneline", "-1").split(" ", 1)[1].startswith("journal: %s" % slug))

    def test_dirty_clone_without_allow_reset_is_soft_fail_and_untouched(self):
        (self.work / "stray.txt").write_text("left over")
        with self.assertRaises(sg.SoftFail):
            sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=False)
        self.assertTrue((self.work / "stray.txt").exists())
        self.assertEqual(len(sg.queue_items(self.work)), 3)

    def test_dirty_non_dedicated_clone_with_allow_reset_is_soft_fail_and_untouched(self):
        """W1: a human's clone (not the dedicated one) is never reset, even with allow_reset: the file stays, SoftFail."""
        (self.work / "stray.txt").write_text("left over")
        logs = []
        with mock.patch.object(sg, "DEDICATED_REPO", self.tmp / "elsewhere"), self.assertRaises(sg.SoftFail):
            sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True, log=logs.append)
        self.assertEqual((self.work / "stray.txt").read_text(), "left over")
        self.assertEqual(len(sg.queue_items(self.work)), 3)
        self.assertEqual(sorted(p.name for p in (self.work / "content" / "journal").iterdir()), [".gitkeep"])
        self.assertFalse(any("restored" in m for m in logs))

    def test_restore_git_failure_is_soft_fail(self):
        """W1: restore() checks git's return code; a failing reset is a SoftFail, not a silent success."""
        (self.work / ".git" / "index.lock").write_text("")  # makes git reset fail (128)
        try:
            with self.assertRaises(sg.SoftFail):
                sg.restore(self.work)
        finally:
            (self.work / ".git" / "index.lock").unlink()

    def test_dedicated_dirty_restore_failure_is_soft_fail_not_published(self):
        """W1: in the dedicated clone a dirty tree whose restore fails stops the run as SoftFail before anything is published."""
        (self.work / "stray.txt").write_text("left over")
        (self.work / ".git" / "index.lock").write_text("")
        try:
            with mock.patch.object(sg, "DEDICATED_REPO", self.work):
                with self.assertRaises(sg.SoftFail):
                    sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True)
        finally:
            (self.work / ".git" / "index.lock").unlink()
        self.assertTrue((self.work / "stray.txt").exists())
        self.assertEqual(len(sg.queue_items(self.work)), 3)

    def test_main_explicit_repo_passes_allow_reset_false(self):
        """W1 (the opposite case): --repo to a non-dedicated clone, even with --allow-reset, reaches publish with allow_reset=False."""
        seen = []

        def fake(repo, day, hm, **kw):
            seen.append(kw)
            return "empty", None

        with mock.patch.object(sg, "DEDICATED_REPO", self.tmp / "elsewhere"), mock.patch.object(sg, "publish_one", fake), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = sg.main(["--repo", str(self.work), "--allow-reset", "--base", str(self.base), "--now", "2026-11-20T18:45:00+09:00"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0]["allow_reset"], False)

    def test_last_tick_soft_fail_notifies_missed_once(self):
        """review 2: launchd's 18:45:00 tick is due, not expired; a SoftFail there reports today's miss, once per day."""
        self.flag("check_fail")
        g = self.gate(18, 45)
        self.assertEqual(g.tick()[0], "failed")
        self.assertEqual(g.tick()[0], "failed")  # the same minute again (18:45:30): no second notice
        missed = [t for t, m in self.notes if t == "블러쉬랩 발행 놓침"]
        self.assertEqual(len(missed), 1)
        self.assertIn("2026-11-20", [m for t, m in self.notes if t == "블러쉬랩 발행 놓침"][0])

    def test_soft_fail_before_last_minute_does_not_notify_missed(self):
        self.flag("check_fail")
        g = self.gate(18, 44)
        self.assertEqual(g.tick()[0], "failed")
        self.assertEqual(self.notes, [])

    def test_incomplete_head_notifies_once_a_day(self):
        """review 3: an incomplete queue head is a SoftFail with its own once-a-day notice (HEAD_INCOMPLETE), never skipped."""
        head = sg.queue_items(self.work)[0][0]
        _git(self.work, "rm", "-q", str(head / "ja.md"))
        _git(self.work, "commit", "-q", "-m", "drop ja")
        _git(self.work, "push", "-q", "origin", "main")
        g = self.gate(18, 45)
        self.assertEqual(g.tick()[0], "failed")
        self.assertEqual(g.tick()[0], "failed")
        self.assertEqual(len([t for t, _ in self.notes if t == "블러쉬랩 큐 미완성"]), 1)
        self.assertFalse((self.base / "FAILED").exists())
        self.assertEqual(len(sg.queue_items(self.work)), 3)  # head kept, nothing skipped

    def test_window_closed_but_today_on_origin_records_already(self):
        """review 5 / W4: RunAtLoad after the window closed: today's post already on origin/main -> published=true, no miss notice.
        The post is pushed from ANOTHER clone, so this clone's origin/main ref is stale until the tick fetches: without the
        fetch the check would see no post and notify MISSED."""
        other = self.tmp / "other-post"
        _git(self.tmp, "clone", "-q", str(self.origin), str(other))
        _git(other, "config", "user.name", "test")
        _git(other, "config", "user.email", "test@example.invalid")
        post = other / "content" / "journal" / "remote-post"
        post.mkdir(parents=True)
        (post / "en.md").write_text("---\ntitle: x\npublished: 2026-11-20T13:15+09:00\n---\nbody\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "remote post")
        _git(other, "push", "-q", "origin", "main")
        self.assertNotEqual(_git(self.work, "rev-parse", "origin/main").strip(), _git(other, "rev-parse", "HEAD").strip())  # stale on purpose
        sg.save_state(self.base / "slot.json", {"date": "2026-11-20", "time": "13:15", "published": False})
        g = self.gate(19, 10)
        self.assertEqual(g.tick(), ("already", "remote-post"))
        self.assertTrue(sg.load_state(self.base / "slot.json")["published"])
        self.assertEqual([t for t, _ in self.notes if t == "블러쉬랩 발행 놓침"], [])
        self.assertEqual(self.status(), "")

    def test_origin_unreachable_is_no_notice_no_state_then_recovers(self):
        """W2: expired + origin unreachable -> no MISSED, slot.json untouched; origin fetched at most once per 15 min;
        the next reachable check decides (no post -> MISSED once; the same day's later ticks stay quiet)."""
        sg.save_state(self.base / "slot.json", {"date": "2026-11-20", "time": "13:15", "published": False})
        real_fetch, fetched = sg.fetch, []

        def counting(repo, remote="origin"):
            fetched.append(remote)
            return real_fetch(repo, remote)

        with mock.patch.object(sg, "fetch", side_effect=counting):
            g = self.gate(19, 10)
            g.kw["remote"] = "nope"  # unreachable
            self.assertEqual(g.tick()[0], "expired-unknown")
            self.assertEqual(self.notes, [])
            self.assertFalse(sg.load_state(self.base / "slot.json")["published"])
            g.now_fn = lambda: dt.datetime(2026, 11, 20, 19, 11, tzinfo=sg.KST)
            self.assertEqual(g.tick()[0], "expired-unknown")  # inside 15 min: no second fetch
            self.assertEqual(fetched, ["nope"])
            g.kw["remote"] = "origin"
            g.now_fn = lambda: dt.datetime(2026, 11, 20, 19, 25, tzinfo=sg.KST)
            self.assertEqual(g.tick()[0], "expired")  # origin reachable, no post today -> MISSED
            self.assertEqual(fetched, ["nope", "origin"])
            g.now_fn = lambda: dt.datetime(2026, 11, 20, 19, 26, tzinfo=sg.KST)
            self.assertEqual(g.tick()[0], "expired")  # cached check, no fetch, no second notice
            self.assertEqual(fetched, ["nope", "origin"])
        self.assertEqual(len([t for t, _ in self.notes if t == "블러쉬랩 발행 놓침"]), 1)
        self.assertFalse(sg.load_state(self.base / "slot.json")["published"])
        self.assertFalse((self.base / "FAILED").exists())

    def test_launchd_call_path_gets_allow_reset(self):
        """review 4: launchd runs main() with no flags against the dedicated clone; that path must reach publish with allow_reset=True."""
        seen = []

        def fake(repo, day, hm, **kw):
            seen.append(kw)
            return "empty", None

        with mock.patch.object(sg, "DEDICATED_REPO", self.work), mock.patch.object(sg, "publish_one", fake), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = sg.main(["--base", str(self.base), "--now", "2026-11-20T18:45:00+09:00"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["allow_reset"])

    # ---- 5차: day rollover checks origin first (item 1)

    def test_rollover_previous_day_on_origin_is_already_no_notice(self):
        self.remote_post("2026-11-19", "yesterday-post")
        sg.save_state(self.base / "slot.json", {"date": "2026-11-19", "time": "13:15", "published": False})
        g = self.gate(12, 5)
        self.assertEqual(g.tick(roll_only=True)[0], "rolled-only")
        self.assertEqual([t for t, _ in self.notes if t == "블러쉬랩 발행 놓침"], [])

    def test_rollover_previous_day_not_on_origin_notifies_missed_once(self):
        sg.save_state(self.base / "slot.json", {"date": "2026-11-19", "time": "13:15", "published": False})
        g = self.gate(12, 5)
        self.assertEqual(g.tick(roll_only=True)[0], "rolled-only")
        self.assertEqual(self.gate(12, 10).tick(roll_only=True)[0], "rolled-only")  # same day: no roll, no second notice
        missed = [m for t, m in self.notes if t == "블러쉬랩 발행 놓침"]
        self.assertEqual(len(missed), 1)
        self.assertIn("2026-11-19", missed[0])
        self.assertNotIn("확인 불가", missed[0])

    def test_rollover_origin_unknown_notifies_missed_as_unverified(self):
        sg.save_state(self.base / "slot.json", {"date": "2026-11-19", "time": "13:15", "published": False})
        with mock.patch.object(sg, "fetch", side_effect=FileNotFoundError("git")):
            self.assertEqual(self.gate(12, 5).tick(roll_only=True)[0], "rolled-only")
        missed = [m for t, m in self.notes if t == "블러쉬랩 발행 놓침"]
        self.assertEqual(len(missed), 1)
        self.assertIn("2026-11-19", missed[0])
        self.assertIn("확인 불가", missed[0])

    # ---- item 2: the extra ticks after the window re-check origin; ORIGIN_UNKNOWN on the last tick

    def test_19_and_21_recheck_origin_and_2345_unknown_notifies_once(self):
        sg.save_state(self.base / "slot.json", {"date": "2026-11-20", "time": "13:15", "published": False})
        with mock.patch.object(sg, "fetch", side_effect=OSError("no git")):
            self.assertEqual(self.gate(19, 0).tick()[0], "expired-unknown")
            self.assertEqual(self.gate(21, 0).tick()[0], "expired-unknown")
            self.assertEqual([t for t, _ in self.notes], [])  # no MISSED, no ORIGIN_UNKNOWN before the last tick
            self.assertEqual(self.gate(23, 45).tick()[0], "expired-unknown")
            self.assertEqual(self.gate(23, 46).tick()[0], "expired-unknown")  # same day: no second notice
        self.assertEqual([t for t, _ in self.notes], ["블러쉬랩 발행 확인 불가"])
        self.assertFalse(sg.load_state(self.base / "slot.json")["published"])
        self.assertFalse((self.base / "FAILED").exists())

    def test_21_and_23_45_recheck_origin_and_find_today_on_origin(self):
        """the 21:00 tick sees a post on origin/main -> already; the 23:45 tick does not notify at all."""
        self.remote_post("2026-11-20", "late-post")
        sg.save_state(self.base / "slot.json", {"date": "2026-11-20", "time": "13:15", "published": False})
        self.assertEqual(self.gate(21, 0).tick(), ("already", "late-post"))
        self.assertEqual(self.gate(23, 45).tick()[0], "published")  # state now published=true: nothing more
        self.assertEqual(self.notes, [])

    # ---- item 3: any OS error from the origin check is "error", not an uncaught crash

    def test_origin_check_os_error_is_error_result(self):
        g = self.gate(19, 10)
        with mock.patch.object(sg, "fetch", side_effect=FileNotFoundError("git")):
            result, detail = g.origin_check("2026-11-20")
        self.assertEqual(result, "error")
        self.assertIn("FileNotFoundError", detail)

    # ---- item 4: git calls get a timeout and no credential prompt; a timeout is a SoftFail

    def test_git_calls_get_timeout_and_no_terminal_prompt(self):
        calls = []

        def fake(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(sg.subprocess, "run", fake):
            sg.fetch(self.work, "origin")
        cmd, kw = calls[0]
        self.assertEqual(cmd, ["git", "fetch", "--quiet", "origin"])
        self.assertEqual(kw["timeout"], 120)
        self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_git_timeout_is_soft_fail_and_origin_error(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 120)

        with mock.patch.object(sg.subprocess, "run", boom):
            with self.assertRaises(sg.SoftFail) as cm:
                sg.fetch(self.work, "origin")
            self.assertIn("timed out", str(cm.exception))
            result, _ = self.gate(19, 10).origin_check("2026-11-20")
        self.assertEqual(result, "error")

    # ---- item 5: restore() is the one reset point; the original error is logged before any restore

    def test_restore_refuses_a_clone_that_is_not_dedicated(self):
        (self.work / "stray.txt").write_text("left over")
        with mock.patch.object(sg, "DEDICATED_REPO", self.tmp / "elsewhere"):
            with self.assertRaises(sg.SoftFail):
                sg.restore(self.work)
        self.assertEqual((self.work / "stray.txt").read_text(), "left over")

    def test_failed_publish_logs_original_error_before_restore(self):
        self.flag("check_fail")
        logs = []
        with self.assertRaises(sg.SoftFail):
            sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True, log=logs.append)
        self.assertTrue(any("publish failed before commit: SoftFail: check.sh failed" in m for m in logs))
        self.assert_restored()

    def test_restore_refused_still_surfaces_original_error_in_log(self):
        self.flag("check_fail")
        logs = []
        with mock.patch.object(sg, "DEDICATED_REPO", self.tmp / "elsewhere"):
            with self.assertRaises(sg.SoftFail) as cm:
                sg.publish_one(self.work, D(2026, 11, 20), "13:15", remote="origin", allow_reset=True, log=logs.append)
        self.assertIn("restore refused", str(cm.exception))
        self.assertTrue(any("check.sh failed" in m for m in logs))  # the original error was logged first


class LaunchdAndHelpTest(unittest.TestCase):
    """item 2 (plist) and item 6 (--allow-reset help): plain file checks, no git."""

    def test_plist_is_well_formed_and_runs_the_extra_ticks(self):
        import plistlib
        with open(REPO / "ops" / "rest.ipe.blushlab-publish.plist", "rb") as f:
            pl = plistlib.load(f)
        got = [(d["Hour"], d["Minute"]) for d in pl["StartCalendarInterval"]]
        self.assertEqual(len(got), 31)  # 12:00-18:45 every 15 min (28) + 19:00, 21:00, 23:45
        for hm in ((19, 0), (21, 0), (23, 45), (12, 0), (18, 45)):
            self.assertIn(hm, got)
        self.assertEqual(len(set(got)), len(got))

    def test_allow_reset_help_says_it_is_ignored(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            sg.main(["--help"])
        self.assertIn("무시됨(호환용) — 복구는 전용 클론에서만", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

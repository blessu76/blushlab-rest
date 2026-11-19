"""ops/slot_gate.py: slot draw, one post a day, queue stays out of the build, datePublished carries the time."""
import datetime as dt
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import REPO, build, jsonld, make_site  # noqa: E402

sys.path.insert(0, str(REPO / "ops"))
import slot_gate as sg  # noqa: E402

D = dt.date


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
        self.assertEqual(g.tick()[0], "waiting")
        self.assertEqual(list((repo / "content" / "journal").iterdir()), [])

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


if __name__ == "__main__":
    unittest.main()

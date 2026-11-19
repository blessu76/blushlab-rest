#!/usr/bin/env python3
"""Random-afternoon slot gate for the blushlab.ipe.rest journal — stdlib only (Python 3.8+).

One post a day, at a random time in 12:00-18:45 KST (15-minute steps) whose hour is never the same as yesterday's.
launchd (ops/rest.ipe.blushlab-publish.plist, every 15 min 12:00-18:45 KST) runs this script; most ticks do nothing.

State file (default ~/.ipe/blushlab-publish/slot.json):
  {"date": "YYYY-MM-DD", "time": "HH:MM", "prev_date": ..., "prev_time": ..., "published": true|false}

Each tick:
  1. slot.json date is not today -> draw a new time (hour != yesterday's); yesterday's date/time move to prev_*.
  2. published already true -> nothing.   now < time -> wait.
  3. otherwise publish the FIRST queue post (content/queue/NN-slug/, all 12 languages): it is moved to content/journal/<slug>/
     with `published:` = today + the slot time, then build_site.py -> scripts/check.sh -> git commit -> git push origin main.
     On success published = true. A post already dated today in content/journal also sets published = true (one post a day).
  4. empty queue -> log and stop. A queue of 2 or fewer posts logs a warning (STOCK_ALERT file in the base dir).
  5. a failed check/build restores the working tree and retries next tick; a failed push leaves FAILED in the base dir (human needed).

  python3 ops/slot_gate.py --status        # next scheduled time + queue, touches nothing
  python3 ops/slot_gate.py                 # normal tick
  python3 ops/slot_gate.py --roll-only     # only (re)draw the slot for --now's date
  python3 ops/slot_gate.py --now 2026-11-20T13:00:00+09:00 --dry-run   # test knobs: plans only, changes nothing
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
LANGS = ("en", "ko", "ja", "zh-Hant", "es", "pt-BR", "fr", "de", "id", "th", "vi", "tr")
SLOTS = ["%02d:%02d" % (h, m) for h in range(12, 19) for m in (0, 15, 30, 45)]  # 12:00 .. 18:45
MIN_STOCK = 3  # warn when the queue holds fewer than this
REPO = Path(__file__).resolve().parent.parent
HOME = Path.home() / ".ipe"
DEFAULT_BASE = HOME / "blushlab-publish"
QUEUE_RE = re.compile(r"^(\d+)-([a-z0-9]+(?:-[a-z0-9]+)*)$")


class SoftFail(Exception):
    """retry on the next tick"""


class HardFail(Exception):
    """a human must look (FAILED file)"""


# ---------------------------------------------------------------- pure logic (unit-tested)

def hour_of(hm):
    try:
        return int(str(hm).split(":", 1)[0])
    except (ValueError, TypeError):
        return None


def draw_time(rng, avoid_hour=None):
    """random 'HH:MM' in 12:00-18:45 (15-minute steps), never in `avoid_hour`."""
    return rng.choice([s for s in SLOTS if hour_of(s) != avoid_hour])


def roll(state, today, rng):
    """-> (state for `today`, rolled?). Yesterday's date/time move to prev_*; the new hour differs from yesterday's."""
    if state and state.get("date") == today.isoformat():
        return state, False
    avoid = None
    if state and state.get("date") == (today - dt.timedelta(days=1)).isoformat():
        avoid = hour_of(state.get("time"))
    return {
        "date": today.isoformat(),
        "time": draw_time(rng, avoid),
        "prev_date": state.get("date") if state else None,
        "prev_time": state.get("time") if state else None,
        "published": False,
    }, True


def decide(now, state):
    """-> (action, why). action: published | wait | due. `now` is an aware datetime."""
    now = now.astimezone(KST)
    if state.get("published"):
        return "published", "today's post is already out (%s)" % state.get("time")
    hh, mm = state["time"].split(":")
    if now.timetz().replace(tzinfo=None) < dt.time(int(hh), int(mm)):
        return "wait", "slot opens at %s" % state["time"]
    return "due", "slot %s reached (%s)" % (state["time"], now.strftime("%H:%M"))


def next_slot(now, state):
    """-> (datetime, text) of the next scheduled publish. After today's slot is used (or passed unpublished with an empty queue) -> tomorrow, time not drawn yet."""
    now = now.astimezone(KST)
    if state and state.get("date") == now.date().isoformat() and not state.get("published"):
        hh, mm = state["time"].split(":")
        return dt.datetime.combine(now.date(), dt.time(int(hh), int(mm)), tzinfo=KST), "today"
    tomorrow = now.date() + dt.timedelta(days=1)
    return dt.datetime.combine(tomorrow, dt.time(12, 0), tzinfo=KST), "tomorrow (time drawn at 12:00-18:45, hour != today's)"


def stock_warning(n):
    if n >= MIN_STOCK:
        return None
    return "WARNING: queue has %d post%s left (min %d) — add more posts to content/queue/" % (n, "" if n == 1 else "s", MIN_STOCK)


def queue_items(root):
    """queue dirs in order (NN-slug sorted). -> [(dir, slug, missing_langs)]"""
    q = Path(root) / "content" / "queue"
    out = []
    for d in sorted(p for p in q.iterdir() if p.is_dir()) if q.is_dir() else []:
        m = QUEUE_RE.match(d.name)
        if not m:
            continue
        out.append((d, m.group(2), [l for l in LANGS if not (d / (l + ".md")).is_file()]))
    return out


def ready_items(root):
    return [(d, s) for d, s, miss in queue_items(root) if not miss]


def stamp_published(text, iso):
    """insert `published: <iso>` into a queue file's front matter (idempotent-hostile: refuses if one is there)."""
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise ValueError("no front matter")
    end = lines.index("---", 1)
    if any(l.startswith("published:") for l in lines[1:end]):
        raise ValueError("already has published:")
    lines.insert(end, "published: %s" % iso)
    return "\n".join(lines)


def published_iso(day, hm):
    return "%sT%s+09:00" % (day.isoformat(), hm)


def post_dated(root, day):
    """slug of a journal post already dated `day` (any language file), else None."""
    j = Path(root) / "content" / "journal"
    for f in sorted(j.glob("*/en.md")) if j.is_dir() else []:
        if re.search(r"^published:\s*%s" % re.escape(day.isoformat()), f.read_text(encoding="utf-8"), re.M):
            return f.parent.name
    return None


# ---------------------------------------------------------------- state file

def load_state(path):
    p = Path(path)
    if not p.is_file():
        return None
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        return s if isinstance(s, dict) and s.get("date") and s.get("time") else None
    except (ValueError, OSError):
        return None


def save_state(path, state):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(p))


# ---------------------------------------------------------------- publishing

def run(cmd, cwd, what):
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise SoftFail("%s failed (%d): %s" % (what, r.returncode, (r.stdout + r.stderr).strip()[-600:]))
    return r.stdout


def publish_one(repo, day, hm, remote="origin", branch="main", dry_run=False, pull=True):
    """queue -> journal -> build -> check -> commit -> push. -> (status, slug). status: published | empty | waiting | already"""
    repo = Path(repo)
    already = post_dated(repo, day)
    if already:
        return "already", already
    items = queue_items(repo)
    if not items:
        return "empty", None
    ready = ready_items(repo)
    if not ready:
        return "waiting", None
    src, slug = ready[0]
    iso = published_iso(day, hm)
    if dry_run:
        return "published", "%s (dry-run, would stamp %s)" % (slug, iso)
    if pull:
        if run(["git", "status", "--porcelain"], repo, "git status").strip():
            raise SoftFail("working tree is dirty — refusing to publish")
        run(["git", "pull", "--rebase", remote, branch], repo, "git pull --rebase")
        src = repo / "content" / "queue" / src.name
        if not src.is_dir():
            raise SoftFail("queue entry vanished after pull")
    dest = repo / "content" / "journal" / slug
    try:
        dest.mkdir(parents=True)
        for l in LANGS:
            (dest / (l + ".md")).write_text(stamp_published((src / (l + ".md")).read_text(encoding="utf-8"), iso), encoding="utf-8")
        shutil.rmtree(str(src))
        run([sys.executable, "scripts/build_site.py"], repo, "build_site.py")
        run(["bash", "scripts/check.sh"], repo, "check.sh")
    except Exception:
        if pull:  # restore the clone: back to HEAD, drop the new files
            subprocess.run(["git", "checkout", "--", "."], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "clean", "-fdq", "content", "journal", "ko", "ja", "zh-Hant", "es", "pt-BR", "fr", "de", "id", "th", "vi", "tr", "en"],
                           cwd=str(repo), capture_output=True)
        raise
    run(["git", "add", "-A"], repo, "git add")
    run(["git", "commit", "-m", "journal: %s (%s)" % (slug, iso)], repo, "git commit")
    try:
        run(["git", "push", remote, branch], repo, "git push")
    except SoftFail as e:
        raise HardFail(str(e))  # the commit exists locally: a human decides (rebase / retry)
    return "published", slug


class Gate:
    def __init__(self, state_path, repo, base, publish_fn=None, now_fn=None, rng=None, log=print, **kw):
        self.state_path, self.repo, self.base = Path(state_path), Path(repo), Path(base)
        self.rng, self.log, self.kw = rng or random.Random(), log, kw
        self.now_fn = now_fn or (lambda: dt.datetime.now(KST))
        self.publish_fn = publish_fn or publish_one

    def now(self):
        return self.now_fn().astimezone(KST)

    def say(self, msg):
        self.log("[%s] %s" % (self.now().strftime("%Y-%m-%d %H:%M:%S KST"), msg))

    def warn_stock(self):
        n = len(ready_items(self.repo))
        msg = stock_warning(n)
        flag = self.base / "STOCK_ALERT"
        if not msg:
            if flag.exists():
                flag.unlink()
            return n
        self.say(msg)
        self.base.mkdir(parents=True, exist_ok=True)
        flag.write_text("%s\n%s\n" % (self.now().date().isoformat(), msg), encoding="utf-8")
        return n

    def tick(self, roll_only=False):
        """-> (action, detail). action: rolled-only | published | wait | published-now | already | empty | waiting | blocked | failed"""
        now = self.now()
        state, rolled = roll(load_state(self.state_path), now.date(), self.rng)
        if rolled:
            save_state(self.state_path, state)
            self.say("new day %s: slot drawn %s KST (prev %s %s)" % (now.date(), state["time"], state["prev_date"], state["prev_time"]))
            self.warn_stock()
        if roll_only:
            return "rolled-only", state["time"]
        action, why = decide(now, state)
        if action in ("published", "wait"):
            self.say("%s: %s" % (action, why))
            return action, why
        if (self.base / "FAILED").exists():
            self.say("FAILED file present — waiting for a human (%s)" % (self.base / "FAILED"))
            return "blocked", "FAILED"
        self.say("due: " + why)
        self.warn_stock()
        try:
            status, slug = self.publish_fn(self.repo, now.date(), state["time"], **self.kw)
        except HardFail as e:
            self.say("HARD FAIL: %s" % e)
            self.base.mkdir(parents=True, exist_ok=True)
            (self.base / "FAILED").write_text("%s\n%s\n" % (now.isoformat(), e), encoding="utf-8")
            return "failed", str(e)
        except SoftFail as e:
            self.say("soft fail: %s (retry next tick)" % e)
            return "failed", str(e)
        if status in ("published", "already"):
            if not self.kw.get("dry_run"):
                state["published"] = True
                save_state(self.state_path, state)
            self.say("%s: %s — slot.json published=true" % (status, slug))
            self.warn_stock()
            return ("published-now" if status == "published" else "already"), slug
        if status == "waiting":
            self.say("queue has no complete entry (12 languages) while a slot was due — nothing published")
            return "waiting", None
        self.say("queue empty while a slot was due — nothing published")
        return "empty", None


def locked(base, fn):
    lock = Path(base) / "gate.lock"
    Path(base).mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            os.kill(int((lock / "pid").read_text().strip()), 0)
            return None  # a live run is in progress
        except (OSError, ValueError):
            shutil.rmtree(str(lock), ignore_errors=True)
            lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    try:
        return fn()
    finally:
        shutil.rmtree(str(lock), ignore_errors=True)


def status(gate, state_path):
    st = load_state(state_path)
    now = gate.now()
    gate.say("slot.json: %s" % json.dumps(st, ensure_ascii=False))
    when, which = next_slot(now, st)
    print("next publish: %s KST (%s)" % (when.strftime("%Y-%m-%d %H:%M"), which))
    if st and st.get("date") == now.date().isoformat():
        print("decision now: %s" % (decide(now, st),))
    q = queue_items(gate.repo)
    print("queue: %d post(s) ready" % len(ready_items(gate.repo)))
    for d, slug, miss in q:
        print("  %s%s" % (d.name, "  [INCOMPLETE: missing %s]" % ",".join(miss) if miss else ""))
    w = stock_warning(len(ready_items(gate.repo)))
    if w:
        print(w)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("BLUSHLAB_PUBLISH_REPO", str(REPO)))
    ap.add_argument("--base", default=os.environ.get("BLUSHLAB_PUBLISH_BASE", str(DEFAULT_BASE)), help="state dir: slot.json, FAILED, STOCK_ALERT")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--now", help="ISO time with offset (test knob), e.g. 2026-11-20T13:00:00+09:00")
    ap.add_argument("--dry-run", action="store_true", help="plan only: no file, git or state change")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--roll-only", action="store_true")
    a = ap.parse_args(argv)
    state_path = Path(a.base) / "slot.json"
    now_fn = (lambda: dt.datetime.fromisoformat(a.now)) if a.now else None
    gate = Gate(state_path, a.repo, a.base, now_fn=now_fn, remote=a.remote, dry_run=a.dry_run)
    if a.status:
        return status(gate, state_path)
    if a.dry_run:  # a dry run must not touch the real slot.json
        gate.state_path = Path(a.base) / "slot.dryrun.json"
        save_state(gate.state_path, load_state(state_path) or {"date": "1970-01-01", "time": "12:00"})
    res = locked(a.base, lambda: gate.tick(roll_only=a.roll_only))
    return 0 if res is None or res[0] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())

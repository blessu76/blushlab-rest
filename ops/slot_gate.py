#!/usr/bin/env python3
"""Random-afternoon slot gate for the blushlab.ipe.rest journal — stdlib only (Python 3.8+).

One post a day, at a random time in 12:00-18:45 KST (15-minute steps) whose hour is never the same as yesterday's.
launchd (ops/rest.ipe.blushlab-publish.plist, every 15 min 12:00-18:45 KST) runs this script; most ticks do nothing.

State files in the base dir (default ~/.ipe/blushlab-publish/):
  slot.json   {"date": "YYYY-MM-DD", "time": "HH:MM", "prev_date": ..., "prev_time": ..., "published": true|false}
  FAILED      present = a human must look; publishing stops until it is removed
  alerts.json last macOS notice day per alert key (FAILED, MISSED, NOCLONE) — each key notifies at most once a day
  STOCK_ALERT queue is short (2 or fewer posts)

Each tick:
  1. slot.json date is not today -> draw a new time (hour != yesterday's); yesterday's date/time move to prev_*.
     A previous day that was never published -> one MISSED notice.
  2. published already true -> nothing.   now < time -> wait.
     now after the window end (18:45, judged to the minute: 18:45:30 is still inside) -> expired, MISSED notice once.
  3. otherwise publish the FIRST queue post (content/queue/NN-slug/, all 12 languages). The head is never skipped: an
     incomplete head is a SoftFail (retry next tick). It is moved to content/journal/<slug>/ with `published:` = today +
     the slot time, then build_site.py -> scripts/check.sh -> git commit -> git push origin main.
     On success published = true. A post already dated today in content/journal also sets published = true (one post a day).
  4. empty queue -> log and stop. A queue of 2 or fewer posts logs a warning (STOCK_ALERT file in the base dir).
  5. A publish run refuses to start while this clone has commits that origin/main does not (an earlier push did not go
     through). The pull is part of the restored step: a pull failure aborts the rebase and retries next tick.
     A failed check/build/pull restores the working tree (when reset is allowed) and retries next tick.
     Any unexpected exception is logged with its traceback, restored the same way, and retried next tick.
     A failed commit restores the tree and is a HardFail (FAILED). A failed push keeps the local commit, writes its hash to
     FAILED, and is a HardFail: a human pushes or drops that commit, then removes FAILED.
     "Already published today" is judged on origin/main after `git fetch`.

  ⚠️ The default repo is the dedicated clone ~/.ipe/blushlab-publish/repo. Recovery runs `git reset --hard` and `git clean`,
     so nobody works by hand in that clone. --repo and BLUSHLAB_PUBLISH_REPO are for tests: they pick another clone, and
     a publish run there resets or cleans only with --allow-reset. A missing clone is logged and notified, not a crash.

  python3 ops/slot_gate.py --status --repo .   # next scheduled time + queue, touches nothing
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
import traceback
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
LANGS = ("en", "ko", "ja", "zh-Hant", "es", "pt-BR", "fr", "de", "id", "th", "vi", "tr")
SLOTS = ["%02d:%02d" % (h, m) for h in range(12, 19) for m in (0, 15, 30, 45)]  # 12:00 .. 18:45
MIN_STOCK = 3  # warn when the queue holds fewer than this
WINDOW_END = dt.time(18, 45)  # last slot; a missed slot is published only up to this clock time (minute precision)
REPO = Path(__file__).resolve().parent.parent
HOME = Path.home() / ".ipe"
DEFAULT_BASE = HOME / "blushlab-publish"
DEDICATED_REPO = DEFAULT_BASE / "repo"  # the default publishing clone
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


def minute_clock(now):
    """wall clock at minute precision: seconds never count (18:45:30 is still the 18:45 slot)."""
    return dt.time(now.hour, now.minute)


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
    """-> (action, why). action: published | wait | due | expired. `now` is an aware datetime."""
    now = now.astimezone(KST)
    if state.get("published"):
        return "published", "today's post is already out (%s)" % state.get("time")
    hh, mm = state["time"].split(":")
    clock = minute_clock(now)
    if clock < dt.time(int(hh), int(mm)):
        return "wait", "slot opens at %s" % state["time"]
    if clock > WINDOW_END:  # a slot missed while the Mac slept: publish only inside the window
        return "expired", "slot %s missed and the window closed at %s — nothing today" % (state["time"], WINDOW_END.strftime("%H:%M"))
    return "due", "slot %s reached (%s)" % (state["time"], now.strftime("%H:%M"))


def next_slot(now, state):
    """-> (datetime, text) of the next scheduled publish. After today's slot is used (or passed unpublished with an empty queue) -> tomorrow, time not drawn yet."""
    now = now.astimezone(KST)
    if (state and state.get("date") == now.date().isoformat() and not state.get("published")
            and minute_clock(now) <= WINDOW_END):
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


def queue_head(root):
    """(dir, slug) of the first queue entry. The order is never skipped: an incomplete head is a SoftFail. Queue must not be empty."""
    d, slug, miss = queue_items(root)[0]
    if miss:
        raise SoftFail("queue head %s is incomplete (missing %s) — waiting, order kept" % (d.name, ",".join(miss)))
    return d, slug


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


def fetch(repo, remote="origin"):
    run(["git", "fetch", "--quiet", remote], repo, "git fetch")


def local_ahead(repo, remote="origin", branch="main"):
    """short hashes of commits on this clone's HEAD that <remote>/<branch> does not have (call after fetch)."""
    out = run(["git", "log", "--format=%h", "%s/%s..HEAD" % (remote, branch)], repo, "git log")
    return out.split()


def pull_rebase(repo, remote="origin", branch="main"):
    try:
        run(["git", "pull", "--rebase", remote, branch], repo, "git pull --rebase")
    except SoftFail:
        subprocess.run(["git", "rebase", "--abort"], cwd=str(repo), capture_output=True)  # no-op when no rebase is running
        raise


def origin_post_dated(repo, day, remote="origin", branch="main"):
    """slug of a journal post dated `day` on <remote>/<branch> (call after fetch), else None. Local-only commits do not count."""
    ref = "%s/%s" % (remote, branch)
    r = subprocess.run(["git", "grep", "-l", "-E", "^published:[[:space:]]*%s" % day.isoformat(), ref, "--", "content/journal/*/en.md"],
                       cwd=str(repo), capture_output=True, text=True)
    if r.returncode not in (0, 1):  # 1 = no match
        raise SoftFail("git grep origin failed: %s" % r.stderr.strip()[-300:])
    for line in r.stdout.splitlines():  # "origin/main:content/journal/<slug>/en.md"
        path = line.split(":", 1)[-1].split("/")
        if len(path) == 4 and path[:2] == ["content", "journal"]:
            return path[2]
    return None


def guard_repo(repo, explicit):
    """None when a publishing run may use `repo`, else the refusal text. The default is the dedicated clone; another path needs --repo (tests)."""
    if explicit or Path(repo).resolve() == DEDICATED_REPO.resolve():
        return None
    return "refusing to publish from %s — only the dedicated clone %s may run this (test runs: pass --repo)" % (repo, DEDICATED_REPO)


def notify(title, msg):
    """macOS notification; never raises."""
    esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')[:200]
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s"' % (esc(msg), esc(title))],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def restore(repo):
    """back to HEAD: unstage, drop edits and the new journal files. Only when reset is allowed (see publish_one / --allow-reset)."""
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=str(repo), capture_output=True)


def publish_one(repo, day, hm, remote="origin", branch="main", dry_run=False, pull=True, allow_reset=False):
    """queue -> journal -> build -> check -> commit -> push. -> (status, slug). status: published | empty | already
    A failure after the first file change restores the clone when allow_reset is set. A commit or push failure is HardFail
    (a human looks); a push failure keeps the local commit, which then blocks every later run until it is resolved."""
    repo = Path(repo)
    if pull and not dry_run:
        if run(["git", "status", "--porcelain"], repo, "git status").strip():
            raise SoftFail("working tree is dirty — refusing to publish")
        fetch(repo, remote)
        ahead = local_ahead(repo, remote, branch)
        if ahead:
            raise HardFail("%d local commit(s) not on %s/%s (%s) — an earlier push did not go through. Push or drop them in the clone, then remove FAILED"
                           % (len(ahead), remote, branch, ", ".join(ahead)))
        already = origin_post_dated(repo, day, remote, branch)  # judged on origin/main, not on this clone
    else:
        already = post_dated(repo, day)
    if already:
        return "already", already
    if not queue_items(repo):
        return "empty", None
    _, slug = queue_head(repo)  # SoftFail when the head is incomplete
    iso = published_iso(day, hm)
    if dry_run:
        return "published", "%s (dry-run, would stamp %s)" % (slug, iso)
    try:
        if pull:
            pull_rebase(repo, remote, branch)
        src, slug = queue_head(repo)  # re-checked after the pull: the queue may have moved
        dest = repo / "content" / "journal" / slug
        dest.mkdir(parents=True)
        for l in LANGS:
            (dest / (l + ".md")).write_text(stamp_published((src / (l + ".md")).read_text(encoding="utf-8"), iso), encoding="utf-8")
        shutil.rmtree(str(src))
        run([sys.executable, "scripts/build_site.py"], repo, "build_site.py")
        run(["bash", "scripts/check.sh"], repo, "check.sh")
    except Exception:
        if allow_reset:
            restore(repo)
        raise
    try:
        run(["git", "add", "-A"], repo, "git add")
        run(["git", "commit", "-m", "journal: %s (%s)" % (slug, iso)], repo, "git commit")
    except SoftFail as e:
        if allow_reset:
            restore(repo)  # nothing staged, nothing half-written
        raise HardFail("commit failed%s: %s" % (", clone restored" if allow_reset else "", e))
    sha = run(["git", "rev-parse", "--short", "HEAD"], repo, "git rev-parse").strip()
    try:
        run(["git", "push", remote, branch], repo, "git push")
    except SoftFail as e:
        raise HardFail("push failed — local commit %s kept (not on %s/%s). Push or drop it in the clone, then remove FAILED. %s"
                       % (sha, remote, branch, e))
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

    def alert(self, key, title, msg, token=None):
        """macOS notice at most once per `token` (default: today) for `key`; state in alerts.json. Dry runs never notify."""
        if self.kw.get("dry_run"):
            return
        token = token or self.now().date().isoformat()
        path = self.base / "alerts.json"
        try:
            seen = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (ValueError, OSError):
            seen = {}
        if not isinstance(seen, dict):
            seen = {}
        if seen.get(key) == token:
            return
        seen[key] = token
        notify(title, msg)
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(seen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

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
        """-> (action, detail). action: rolled-only | published | wait | published-now | already | empty | blocked | failed | expired"""
        now = self.now()
        old = load_state(self.state_path)
        state, rolled = roll(old, now.date(), self.rng)
        if rolled:
            save_state(self.state_path, state)
            self.say("new day %s: slot drawn %s KST (prev %s %s)" % (now.date(), state["time"], state["prev_date"], state["prev_time"]))
            if old and old.get("date") and old["date"] < now.date().isoformat() and not old.get("published"):
                self.say("previous day %s was never published" % old["date"])
                self.alert("MISSED", "블러쉬랩 발행 놓침", "%s 슬롯에 발행하지 못했습니다. 로그를 확인하세요." % old["date"], token=old["date"])
            self.warn_stock()
        if roll_only:
            return "rolled-only", state["time"]
        action, why = decide(now, state)
        if action in ("published", "wait"):
            self.say("%s: %s" % (action, why))
            return action, why
        if action == "expired":
            self.say("expired: %s" % why)
            self.alert("MISSED", "블러쉬랩 발행 놓침", "%s 슬롯에 발행하지 못했습니다. 로그를 확인하세요." % state["date"], token=state["date"])
            return action, why
        if (self.base / "FAILED").exists():
            self.say("FAILED file present — waiting for a human (%s)" % (self.base / "FAILED"))
            self.alert("FAILED", "블러쉬랩 발행 멈춤", "FAILED 파일이 있어 발행을 멈췄습니다. 로그를 확인하세요.")
            return "blocked", "FAILED"
        self.say("due: " + why)
        self.warn_stock()
        try:
            status, slug = self.publish_fn(self.repo, now.date(), state["time"], **self.kw)
        except HardFail as e:
            self.say("HARD FAIL: %s" % e)
            self.base.mkdir(parents=True, exist_ok=True)
            (self.base / "FAILED").write_text("%s\n%s\n" % (now.isoformat(), e), encoding="utf-8")
            self.alert("FAILED", "블러쉬랩 발행 실패", str(e)[:150])
            return "failed", str(e)
        except SoftFail as e:
            self.say("soft fail: %s (retry next tick)" % e)
            return "failed", str(e)
        except Exception as e:  # unexpected: log the traceback, restore, and retry next tick (never a crash under launchd)
            self.say("unexpected %s: %s — restoring, retry next tick\n%s" % (type(e).__name__, e, traceback.format_exc()[-1500:]))
            if self.kw.get("allow_reset") and not self.kw.get("dry_run"):
                restore(self.repo)
            return "failed", "%s: %s" % (type(e).__name__, e)
        if status in ("published", "already"):
            if not self.kw.get("dry_run"):
                state["published"] = True
                save_state(self.state_path, state)
            self.say("%s: %s — slot.json published=true" % (status, slug))
            self.warn_stock()
            return ("published-now" if status == "published" else "already"), slug
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
    ap.add_argument("--repo", default=None, help="publish clone (default: the dedicated clone; any other path is for tests)")
    ap.add_argument("--allow-reset", action="store_true",
                    help="let a publish run reset/clean a clone that is not the dedicated one (the dedicated clone always may)")
    ap.add_argument("--base", default=os.environ.get("BLUSHLAB_PUBLISH_BASE", str(DEFAULT_BASE)), help="state dir: slot.json, FAILED, alerts.json, STOCK_ALERT")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--now", help="ISO time with offset (test knob), e.g. 2026-11-20T13:00:00+09:00")
    ap.add_argument("--dry-run", action="store_true", help="plan only: no file, git or state change")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--roll-only", action="store_true")
    a = ap.parse_args(argv)
    state_path = Path(a.base) / "slot.json"
    explicit = bool(a.repo or os.environ.get("BLUSHLAB_PUBLISH_REPO"))
    repo = a.repo or os.environ.get("BLUSHLAB_PUBLISH_REPO") or str(DEDICATED_REPO)
    allow_reset = a.allow_reset or Path(repo).resolve() == DEDICATED_REPO.resolve()
    if not a.status:
        refusal = guard_repo(repo, explicit)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
    now_fn = (lambda: dt.datetime.fromisoformat(a.now)) if a.now else None
    gate = Gate(state_path, repo, a.base, now_fn=now_fn, remote=a.remote, dry_run=a.dry_run, allow_reset=allow_reset)
    if a.status:
        return status(gate, state_path)
    if not Path(repo).is_dir():
        gate.say("전용 클론 없음: %s — 발행하지 않습니다 (클론을 다시 만든 뒤 실행하세요)" % repo)
        gate.alert("NOCLONE", "블러쉬랩 발행 멈춤", "전용 클론이 없습니다: %s" % repo)
        return 1
    if a.dry_run:  # a dry run must not touch the real slot.json
        gate.state_path = Path(a.base) / "slot.dryrun.json"
        save_state(gate.state_path, load_state(state_path) or {"date": "1970-01-01", "time": "12:00"})
    res = locked(a.base, lambda: gate.tick(roll_only=a.roll_only))
    return 0 if res is None or res[0] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())

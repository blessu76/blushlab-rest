"""연락처 메일은 한 주소(scripts/build_site.py 의 EMAIL)로만 쓴다.

privacy.json 의 contactEmail 과 생성·원고 파일 어디에도 다른 메일 주소가 남지 않았는지 검사한다.
주소를 바꿀 때는 EMAIL 한 곳을 고치고 privacy.json 의 contactEmail 도 같은 값으로 맞춘다(이 시험이 어긋남을 잡는다).
"""
import json
import re
import unittest
from pathlib import Path

from helpers import REPO, bs

ADDR_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
SCAN_SUFFIX = {".html", ".md", ".txt", ".json", ".xml", ".js", ".css", ".svg", ".py", ".yml", ".yaml"}
SKIP_DIRS = {".git", "vendor", "tests", "__pycache__", ".github"}


class ContactEmailTest(unittest.TestCase):
    def test_privacy_json_contact_matches_constant(self):
        cfg = json.loads((REPO / "privacy.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["contactEmail"], bs.EMAIL,
                         "privacy.json contactEmail 을 scripts/build_site.py EMAIL(%s) 과 같게 맞춘다" % bs.EMAIL)

    def test_every_address_in_repo_is_the_contact_constant(self):
        found = {}
        for f in REPO.rglob("*"):
            rel = f.relative_to(REPO)
            if not f.is_file() or f.suffix not in SCAN_SUFFIX or any(p in SKIP_DIRS for p in rel.parts):
                continue
            for m in ADDR_RE.findall(f.read_text(encoding="utf-8", errors="ignore")):
                found.setdefault(m, set()).add(str(rel))
        other = {a: sorted(fs)[:3] for a, fs in found.items() if a != bs.EMAIL}
        self.assertEqual(other, {}, "연락처 메일은 %s 로 통일 — 다른 주소가 남아 있다: %s" % (bs.EMAIL, other))
        self.assertIn(bs.EMAIL, found, "연락처 메일이 어디에도 없다")


if __name__ == "__main__":
    unittest.main()

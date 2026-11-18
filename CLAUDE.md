# CLAUDE.md — blushlab-rest

- 정적 사이트 repo. 생성물(HTML·sitemap 등)은 `scripts/build_site.py` 로만 만든다. 손으로 고치지 않는다.
- 커밋 전 `bash scripts/check.sh` 가 GREEN 이어야 한다.
- 색·글꼴은 `assets/blushlab.css` 의 `:root` 토큰으로만 바꾼다. 임시 테마 상태다.
- 언어는 `content/i18n` 의 12개. 비주 언어 페이지는 `translation_of: en` 을 frontmatter 에 둔다.
- 개인정보·약관 문구는 법적 검토 전이며 `[미검증]`·`[법적 확인 필요]` 표시를 함부로 지우지 않는다.
- 문서(기획·설계)는 이 repo에 만들지 않는다.

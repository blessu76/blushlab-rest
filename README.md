# blushlab-rest

Blushlab 공식 정적 사이트 (https://blushlab.ipe.rest). 메이크업 스튜디오와 얼굴 사진 모드를 합친 게임의 소개·지원·정책 페이지입니다.

## 구조
- `scripts/build_site.py` — 표준 라이브러리만 쓰는 정적 생성기. 모든 HTML, sitemap.xml, robots.txt, llms.txt, 404.html, 피드를 만든다.
- `content/i18n/*.json` — 언어별 UI 문구 (12개 언어).
- `content/pages/<lang>/*.md` — about·support·privacy·terms 본문.
- `assets/` — 스타일시트(`blushlab.css`), 언어 전환 스크립트, 파비콘, 공유 이미지 원본.
- `vendor/ipe_web_kit/` — 비공개 공용 키트 (git 무시, `scripts/kit_lock.py sync` 로 동기화).

## 실행
```
python3 scripts/build_site.py   # 생성
bash scripts/check.sh           # 그린 게이트 (키트·원본·단위 테스트·생성본 일치·sitemap)
```

## 주의
- 색 토큰은 `assets/blushlab.css` 의 `:root` 변수에만 둔다. 현재 값은 임시 테마이며 디자인 v1 확정 후 교체한다.
- 스타일시트 링크에는 `?v=ASSET_V` 캐시 버스팅을 붙인다 (`build_site.py` 의 `ASSET_V`).
- 문서는 이 repo에 두지 않는다. 기획·설계 문서는 `_ipe/foundry/` 쪽에 둔다.

# 오토워커 위키 (wiki.autoworkers.ai)

노션에 쓴 강의 자료를 그대로 가져와 정적 사이트로 만든다. **노션에서 수정하면 30분 안에 사이트에 반영된다.**

```
노션 (공통 · 프리미엄반)
   │  sync.py — 노션 API로 페이지·DB·이미지 크롤링 → .cache/
   ▼
build.py — HTML 생성 → _site/
   ▼
GitHub Actions (30분마다) → GitHub Pages → wiki.autoworkers.ai
```

## 로컬에서 돌려보기

```bash
python3 sync.py      # .env 의 NOTION_TOKEN 필요
python3 build.py
python3 -m http.server 8766 --directory _site   # http://localhost:8766
```

## 설정 — `config.json`

| 키 | 뜻 |
|---|---|
| `sections` | 사이트 섹션 = 노션 루트 페이지 (slug가 URL: `/common/`, `/premium/`) |
| `curriculum_db` | 이 DB는 월별 일정표로 렌더링 + 홈 "다가오는 일정" |
| `private_feedback.enabled` | 1:1 피드백 명단 포함 여부 (기본 꺼짐 — 실명·이메일 포함) |
| `hide_properties` / `hide_property_types` | 사이트에 절대 노출하지 않을 DB 속성 |

새 섹션을 붙이려면 노션 페이지에 `오토워커 위키` 연결을 추가하고 `sections`에 한 줄 넣으면 된다.

## 즉시 반영

```bash
gh workflow run deploy.yml -R nossssi/autoworker-wiki
```

## 비밀값 (GitHub Secrets)

- `NOTION_TOKEN` — 노션 내부 통합(읽기 전용) 토큰
- `LINK_SALT` — 비공개 링크용 임의 문자열 (1:1 피드백 켤 때만)

1기 위키는 `legacy/gen1/` → `/gen1/` 으로 그대로 배포된다.

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
| `sections` | 노션 루트 페이지 사전 (2기·1기 공통/프반) |
| `editions` | 판 = 섹션 묶음 + 비밀 경로 `/<prefix>-<HMAC(LINK_SALT, key)[:12]>/`. 루트(/)는 안내 페이지만 |
| `editions[].curriculum_db` | 이 DB는 월별 일정표로 렌더링 + 홈 "다가오는 일정" |
| `private_feedback.enabled` | 1:1 피드백 명단 포함 여부 (기본 꺼짐 — 실명·이메일 포함) |
| `hide_properties` / `hide_property_types` | 사이트에 절대 노출하지 않을 DB 속성 |

새 기수를 붙이려면 노션 페이지에 `오토워커위키` 연결을 추가하고 `sections`·`editions`에 추가한다.
판 주소는 로컬에서 `python3 build.py` 출력으로만 확인한다 (공개 저장소라 Actions 로그엔 찍지 않음).
`check_leaks.py`가 판끼리 경로·전용 페이지가 섞이지 않았는지 배포 전에 검사한다.

## 노션 변경 감시 (2~3분 내 반영)

`watch.py`가 1분마다 노션 검색 API에 "최근 수정순"을 1회 물어보고, 새 수정이 있으면 배포 워크플로를 실행한다.
대표 맥북에 launchd로 상시 등록되어 있다 (`~/Library/LaunchAgents/ai.autoworkers.wiki-watch.plist`, 로그 `.cache/watch.log`).
맥북이 꺼져 있을 땐 GitHub Actions 30분 예약 실행이 대신한다. 삭제(휴지통)는 검색에 안 잡혀 30분 실행에서 반영된다.

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.autoworkers.wiki-watch.plist    # 끄기
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.autoworkers.wiki-watch.plist  # 켜기
python3 watch.py --loop   # 다른 컴퓨터에서 임시로 돌릴 때
```

## 즉시 반영

```bash
gh workflow run deploy.yml -R nossssi/autoworker-wiki
```

## 비밀값 (GitHub Secrets)

- `NOTION_TOKEN` — 노션 내부 통합(읽기 전용) 토큰
- `LINK_SALT` — 비공개 링크용 임의 문자열 (1:1 피드백 켤 때만)

1기 위키는 `legacy/gen1/` → `/gen1/` 으로 그대로 배포된다.

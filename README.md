# price-monitoring

폐쇄몰 특판 상품의 공개 노출과 표시가격을 4시간 단위로 수집하고, Google Drive에 검증·보존하는 프로젝트입니다.

## 산출물 경계

모든 업무 백업은 `Work backup/가격 모니터링` 아래에서만 관리합니다.

| 경로 | 내용 | 쓰기 주체 | 성격 |
|---|---|---|---|
| `history/YYYY/MM/DD` | 실행별 원본 JSON | GitHub Actions → Apps Script | 불변 원본 |
| `current/latest.json` | 최신 원본의 ID·URL·해시 | Apps Script | 갱신 가능한 포인터 |
| `briefings/YYYY/MM` | 08시 규칙 기반 기본 브리핑 | GitHub Actions → Apps Script | 기계 1차 자료 |
| `analysis-input/YYYY/MM` | 당일 수집 결과를 정규화한 AI용 단일 입력 문서 | GitHub Actions → Apps Script | 최근 3일 대조용 |
| `final-briefings/YYYY/MM` | 심층 검증·보완·출력용 최종 브리핑 | Codex | 사람용 최종본 |
| `lessons` | 오류 원인과 대응책 | Codex | 누적 운영 지식 |

`briefings`의 문서는 최종본이 아닙니다. 최종 판단과 배포는 반드시 `final-briefings`의 문서만 사용합니다. Apps Script는 `final-briefings`에 쓰지 않으며, Codex는 `history`의 원본을 수정하지 않습니다.

## 처리 흐름

1. GitHub Actions가 공개 검색 결과와 접근 가능한 판매 페이지를 API/HTML/JSON 방식으로 수집합니다.
2. 수집기는 상품명을 분류하고 가격 규칙을 적용해 원본 JSON을 만듭니다.
3. Apps Script가 공유 비밀값, JSON 구조, 실행 ID, SHA-256을 검증합니다.
4. 검증된 JSON을 실행별 불변 파일로 Drive `history`에 저장하고 `current/latest.json` 포인터만 갱신합니다.
5. 08시 회차에는 규칙 기반 기본 브리핑과 AI용 통합 입력 문서를 각각 `briefings`, `analysis-input`에 저장합니다.
6. 2차 분석 AI는 `analysis-input`의 최근 3일 문서를 우선 대조하고, 식별 충돌 등 예외가 있을 때만 원본을 추가 확인합니다.
7. 최종 분석 결과는 `final-briefings`에 별도 최종본으로 작성합니다.

동일 이름의 불변 원본이 이미 있고 해시가 다르면 Apps Script가 저장을 거부하므로 조용한 덮어쓰기가 발생하지 않습니다.

## 1회 활성화 절차

1. [script.google.com](https://script.google.com/)에서 새 프로젝트를 만들고 `apps-script/Code.gs`와 `appsscript.json`을 반영합니다.
2. Apps Script 프로젝트 설정의 스크립트 속성에 `SHARED_SECRET`을 충분히 긴 임의 문자열로 저장합니다.
3. 웹 앱으로 배포합니다. 실행 사용자는 본인, 접근 권한은 웹 요청을 보낼 수 있는 범위로 설정합니다.
4. GitHub 저장소 `Settings → Secrets and variables → Actions`에 다음 저장소 비밀값을 추가합니다.
   - `DRIVE_WEBHOOK_URL`: 배포된 Apps Script 웹 앱 `/exec` URL
   - `DRIVE_SHARED_SECRET`: Apps Script의 `SHARED_SECRET`과 같은 값
5. Actions에서 `Collect price exposure`를 `force_briefing`과 함께 수동 실행하고 Drive의 `history`, `current`, `briefings`, `analysis-input` 생성을 확인합니다.
6. 성공 확인 뒤 `.github/workflows/collect.yml`의 `schedule` 주석을 해제합니다.

GitHub cron은 UTC 기준 `0 3,7,11,15,19,23 * * *`이며 한국시간 12·16·20·00·04·08시에 해당합니다. 예약 실행은 혼잡 시 수분 지연될 수 있지만 결과의 `scheduled_slot_hour_kst`는 해당 4시간 구간으로 기록됩니다.

## 현재 규칙

- 정관장 홍삼본정: 허용 예외 URL 외 노출 금지. 예외가 아직 등록되지 않았으므로 현재는 모든 탐지를 `review_required`로 기록합니다.
- 정관장 홍삼보가: 80,000원 이상 정상, 78,000~79,999원 경고, 78,000원 미만 치명.
- 배송비는 상품가에 임의 합산하지 않고 `79,000원 (+3,000원)` 또는 `79,000원 (배송비 확인불가)`처럼 별도 표기합니다.
- 모델번호와 바코드는 검색하지 않습니다.
- `본정스틱` 등 유사 상품은 제외 규칙으로 분리합니다.

규칙과 검색처는 `config/monitoring.json`에서 확장합니다.

## 로컬 검증

```bash
python -m unittest discover -s tests -v
python src/monitor.py --no-upload --force-briefing
```

수집기는 공개적으로 접근 가능한 페이지와 정상 HTTP 요청만 사용합니다. 접근 제한을 우회하지 않으며, 차단·타임아웃·구조 변경은 원본 JSON의 `errors`에 기록해 최종 분석 단계에서 보완합니다.

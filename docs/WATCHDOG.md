# 수집 Watchdog

수집기와 분리된 GitHub Actions 작업이 매 수집 회차의 재시도 시간 이후 Google Drive를 확인합니다.

## 실행 시각

- 수집: KST 00/04/08/12/16/20시 `:07`
- 조건부 재시도: 같은 회차 `:17`
- Watchdog: 같은 회차 `:45`

Watchdog은 재시도 시작 시각으로부터 28분의 여유를 둡니다. 정상 회차도 로그를 남기며, 누락된 회차만 사고 파일을 생성하고 GitHub Actions 실행을 실패 상태로 표시합니다.

## Drive 산출물

- `logs/watchdog/YYYY/MM/DD/watchdog-YYYYMMDD-HH.json`: 회차별 정상·누락 검사 결과
- `incidents/YYYY/MM/DD/missing-slot-YYYYMMDD-HH.json`: 누락 회차의 최초 사고 기록

파일명은 날짜와 회차로 고정됩니다. 동일 회차를 수동으로 다시 검사해도 최초 파일을 유지하므로 중복 사고가 생성되지 않습니다.

## 활성화 절차

1. GitHub의 `apps-script/Code.gs` 전체 내용을 Apps Script 프로젝트에 반영합니다.
2. 웹 앱에서 **새 버전**을 배포합니다. 이번 버전은 이메일 기능을 사용하지 않으므로 이메일 발송 권한이 필요하지 않습니다.
3. GitHub Actions에서 `Watch collection health`를 수동 실행합니다.
4. 이미 수집된 날짜와 회차를 지정해 정상 로그가 Drive `logs/watchdog/`에 생성되는지 확인합니다.
5. 누락 시험은 과거의 실제 미수집 회차 또는 별도 시험 날짜를 지정합니다. `incidents/` 파일과 실패한 Actions 실행이 모두 생성되어야 합니다.

이메일이나 별도 푸시 알림은 현재 범위에 포함하지 않습니다. 향후 알림 채널을 결정한 뒤 별도 변경으로 추가합니다.

## 판정 원칙

- `history/YYYY/MM/DD`에서 `scheduled_slot_hour_kst`가 일치하는 불변 수집 원본이 하나라도 발견되면 정상입니다.
- 검색 결과가 0개여도 수집 원본이 정상 저장됐다면 Watchdog 관점에서는 정상입니다.
- 수집 원본 자체가 없을 때만 누락 사고입니다.
- Drive 게이트웨이에 접근할 수 없거나 응답이 손상된 경우에는 판정을 추측하지 않고 Actions 실행을 실패시킵니다.

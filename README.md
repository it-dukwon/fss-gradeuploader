# FSS Grade Uploader

축산물원패스(ekape.or.kr)에서 돼지도체 등급판정결과 엑셀을 자동 다운로드하고,
Azure Data Lake Storage(ADLS)에 업로드하는 자동화 도구입니다.
실행 결과는 fss-webapp API로 로그를 전송합니다.

## 동작 흐름

1. 축산물원패스 로그인 (거래증명통합)
2. 돼지도체위임현황 메뉴 진입
3. 판정기간을 어제 날짜로 설정 후 조회
4. 각 행의 등급판정결과 엑셀 다운로드 (기계판정 제외)
5. 다운로드된 엑셀 파일을 ADLS에 업로드
6. 실행 결과 로그를 fss-webapp API로 전송
7. **(자동 소급)** 실패 누적 날짜를 `state/failed_dates.json`에서 읽어 retry. 7일 경과분은 자동 포기.

### 재시도 동작 (2단계)

- **잡 단위 재시도**: 다운로드/업로드가 일시적 실패(타임아웃·메뉴이동·조회·업로드 오류)면 **5분 간격으로 최대 2회 재시도**(총 3회 시도). `no_data`(0건)·`partial`(일부 업로드)·`config_missing`(계정 누락)은 재시도하지 않음. 간격/횟수는 `.env`의 `RETRY_INTERVAL_SEC`/`RETRY_MAX`로 조정.
- **일 단위 소급**: 그래도 실패하면 다음날 자동 재시도(위 7번, 최대 7일).
- **로그 표기**: webapp 로그의 `error_message`에 시도 정보를 함께 보냄. 성공이어도 중간 실패가 있었으면 `(참고) N회 시도 중 M회 실패 후 성공...`으로 남고, 최종 실패는 `[N회 시도 모두 실패] ...`로 기록됨. **성공/실패 판정은 `status` 필드 기준** (error_message 존재 여부로 판단 금지).

> 로그인 페이지 진입은 `domcontentloaded` 기준으로 대기함. (과거 `networkidle`은 Nexacro 백그라운드 통신 때문에 간헐적으로 60초 내 idle에 도달 못 해 타임아웃 발생 → 2026-05-30/06-07 실패. 폐기.)

## 실행 환경

- **운영 서버**: Azure VM (`fss-gradeuploader`, Ubuntu)
- **스케줄**: cron으로 매일 KST 08:00 (UTC 23:00) 자동 실행
- **VM 자동 시작/종료**: Azure Runbook으로 KST 07:55 시작, 08:30 종료

## 폴더 구조

```
fss-gradeuploader/
├── main.py                # 메인 실행 (다운로드 → 업로드 → 로그 전송 → 실패 소급)
├── download_grades.py     # 축산물원패스 다운로드 로직
├── upload_grades.py       # ADLS 업로드 로직
├── failed_dates.py        # 실패 날짜 추적 (record/clear/pending_for_retry)
├── .env                   # 환경변수 (로그인 정보, ADLS, API 설정)
├── requirements.txt       # Python 패키지 목록
├── downloads/             # 다운로드된 엑셀 파일 (날짜별)
├── state/                 # 런타임 상태 (failed_dates.json — 자동 생성, gitignore)
└── logs/                  # 실행 로그
```

## Azure VM 초기 설정

```bash
git clone https://github.com/it-dukwon/fss-gradeuploader.git
cd ~/fss-gradeuploader
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
mkdir -p logs downloads
```

## 환경변수 (.env)

```env
# 축산물원패스 로그인
EKAPE_ID=아이디
EKAPE_PW=비밀번호

# Azure ADLS 업로드
AZURE_STORAGE_CONNECTION_STRING=연결문자열
AZURE_STORAGE_CONTAINER=컨테이너명

# headless 모드 (VM에서는 true 필수)
CI=true

# 잡 단위 재시도 (선택 — 미설정 시 기본 5분 간격 2회). VM 가동창(07:55~08:30) 안에 끝나야 함
RETRY_INTERVAL_SEC=300
RETRY_MAX=2

# 실행 로그 전송 (설정된 환경에만 전송, 없으면 건너뜀)
FSS_WEBAPP_API_URL=https://webapp-databricks-dashboard-xxx.azurewebsites.net
FSS_WEBAPP_API_KEY=api키값
FSS_WEBAPP_API_URL_DEV=https://fss-webapp-dev-xxx.azurewebsites.net
FSS_WEBAPP_API_KEY_DEV=api키값
FSS_WEBAPP_API_URL_PRD=https://fss-webapp-prd-xxx.azurewebsites.net
FSS_WEBAPP_API_KEY_PRD=api키값
```

## 사용법

### 수동 실행 (VM)

```bash
cd ~/fss-gradeuploader
source venv/bin/activate
python3 main.py
```

특정 날짜 지정 (수동 백필):
```bash
TARGET_DATE=2026-03-28 python3 main.py
```
> 셸 env로 `TARGET_DATE`를 주입하면 그 날짜 1회만 처리하고 자동 소급 백필은 **스킵**됩니다 (특정 날짜만 돌고 싶을 때 간섭 안 하게).
> cron 평상 실행 (`TARGET_DATE` 미설정)에서는 어제분 처리 후 `state/failed_dates.json`의 누적 실패를 자동 retry합니다.

### cron 스케줄 확인/수정

```bash
crontab -l
crontab -e
```

현재 설정:
```
0 23 * * * cd ~/fss-gradeuploader && /home/dnftksdodi/fss-gradeuploader/venv/bin/python3 main.py >> /home/dnftksdodi/fss-gradeuploader/logs/cron_$(date +\%Y\%m\%d).log 2>&1
```

### 소스 업데이트 (push 후 VM 반영)

```bash
cd ~/fss-gradeuploader
git pull
source venv/bin/activate
pip install -r requirements.txt
```

## 실행 확인

```bash
# 로그 파일 목록
ls -lt ~/fss-gradeuploader/logs/

# 최신 로그 확인
tail -50 ~/fss-gradeuploader/logs/cron_$(date +%Y%m%d).log

# 오류 검색
grep -i "error\|오류\|실패" ~/fss-gradeuploader/logs/run_*.log

# 다운로드 파일 확인
ls ~/fss-gradeuploader/downloads/
```

## 문제 해결

### VM에서 로그인 타임아웃 (`Page.goto: Timeout ...`)
- 잡 단위 재시도(5분×2)가 자동으로 한 번 더 시도하므로 일시적 지연은 자체 복구됨. 로그에서 `[시도 n/3]` 확인.
- 계속 실패 시: `.env`의 `EKAPE_ID`/`EKAPE_PW` 확인, `curl -I https://www.ekape.or.kr`로 네트워크 확인.
- 지금도 재현되는지 단독 점검:
  ```bash
  python3 -c "from playwright.sync_api import sync_playwright as S;pg=S().start().chromium.launch(headless=True).new_context().new_page();pg.goto('https://www.ekape.or.kr/kapecp/ui/kapecp/fastLogin.jsp',wait_until='domcontentloaded',timeout=60000);print('OK')"
  ```

### venv 꼬임 (deactivate 후 재생성)
```bash
deactivate
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

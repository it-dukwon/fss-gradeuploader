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

## 실행 환경

- **운영 서버**: Azure VM (`fss-gradeuploader`, Ubuntu)
- **스케줄**: cron으로 매일 KST 08:00 (UTC 23:00) 자동 실행
- **VM 자동 시작/종료**: Azure Runbook으로 KST 07:55 시작, 08:30 종료

## 폴더 구조

```
fss-gradeuploader/
├── main.py                # 메인 실행 (다운로드 → 업로드 → 로그 전송)
├── download_grades.py     # 축산물원패스 다운로드 로직
├── upload_grades.py       # ADLS 업로드 로직
├── .env                   # 환경변수 (로그인 정보, ADLS, API 설정)
├── requirements.txt       # Python 패키지 목록
├── downloads/             # 다운로드된 엑셀 파일 (날짜별)
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

특정 날짜 지정:
```bash
TARGET_DATE=2026-03-28 python3 main.py
```

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

### VM에서 로그인 타임아웃
- `.env`의 `EKAPE_ID`/`EKAPE_PW` 확인
- `curl -I https://www.ekape.or.kr`로 네트워크 확인

### venv 꼬임 (deactivate 후 재생성)
```bash
deactivate
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

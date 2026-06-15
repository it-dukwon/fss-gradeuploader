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
   > ⚠️ Container Apps Job은 컨테이너가 매 실행 초기화되어 `state/`가 보존되지 않으므로 **현재 자동 소급은 비활성(inert)** 상태다(크래시는 없음). 필요 시 Azure Files를 `/app/state`에 마운트하면 VM과 동일하게 동작한다.

위 1~7은 **축평원(ekape) 잡**(`CRAWL_ONLY=ekape`) 흐름이다. **대법원 사건검색**은 별도 잡(`CRAWL_ONLY=court`)으로 분리되어 독립 스케줄로 돈다 (아래 별도 섹션).

### 재시도 동작 (2단계)

- **잡 단위 재시도**: 다운로드/업로드가 일시적 실패(타임아웃·메뉴이동·조회·업로드 오류)면 **5분 간격으로 최대 2회 재시도**(총 3회 시도). `no_data`(0건)·`partial`(일부 업로드)·`config_missing`(계정 누락)은 재시도하지 않음. 간격/횟수는 `.env`의 `RETRY_INTERVAL_SEC`/`RETRY_MAX`로 조정.
- **일 단위 소급**: 그래도 실패하면 다음날 자동 재시도(위 7번, 최대 7일).
- **로그 표기**: webapp 로그의 `error_message`에 시도 정보를 함께 보냄. 성공이어도 중간 실패가 있었으면 `(참고) N회 시도 중 M회 실패 후 성공...`으로 남고, 최종 실패는 `[N회 시도 모두 실패] ...`로 기록됨. **성공/실패 판정은 `status` 필드 기준** (error_message 존재 여부로 판단 금지).

> 로그인 페이지 진입은 `domcontentloaded` 기준으로 대기함. (과거 `networkidle`은 Nexacro 백그라운드 통신 때문에 간헐적으로 60초 내 idle에 도달 못 해 타임아웃 발생 → 2026-05-30/06-07 실패. 폐기.)

## 실행 환경

- **운영**: Azure Container Apps Jobs (Consumption, Korea Central) — 환경 `cae-fss-crawler`
- **잡 2개** (이미지 1개 `fsswebappacr/fss-crawler:latest`를 `CRAWL_ONLY`로 분기):
  - `fss-crawler-ekape` — 축평원 등급 다운로드/업로드. cron `0 10,14,23 * * *` (KST 19·23·08)
  - `fss-crawler-court` — 대법원 사건 진행현황 수집. cron `0 4,9,23 * * *` (KST 13·18·08)
- **인증**: 사용자 할당 identity `id-fss-crawler` (KV `Key Vault Secrets User`, ACR `AcrPull`). 코드의 `DefaultAzureCredential`이 `AZURE_CLIENT_ID`로 이 identity를 사용.
- **시크릿**: Key Vault 참조(`keyvaultref`)로 env에 주입 — 잡 정의에 평문 없음.
- **cron은 UTC 기준** (KST = UTC + 9). 잡은 실행되면 1회 돌고 종료(scale-to-zero), 평소 컴퓨팅 비용 ~0.
- (구) Azure VM(`fss-gradeuploader`)에서 cron+Runbook으로 운영했으며 Container Apps 이관 후 정리 예정. VM 운영 정보는 `vm` 브랜치 참고.

## 폴더 구조

```
fss-gradeuploader/
├── main.py                # 메인 실행 (CRAWL_ONLY=ekape|court|all 분기; 다운로드→업로드→로그→사건검색→소급)
├── Dockerfile             # Container Apps Job용 이미지 (python-slim + playwright chromium)
├── .dockerignore          # 이미지 빌드 제외 목록 (venv/logs/downloads/.env 등)
├── download_grades.py     # 축산물원패스 다운로드 로직 (KST 컷오프 기반 대상일 산정)
├── upload_grades.py       # ADLS 업로드 로직
├── court_case_search.py   # 대법원 나의사건검색 진행현황 수집 (캡차 OCR)
├── failed_dates.py        # 실패 날짜 추적 (record/clear/pending_for_retry)
├── .env                   # 환경변수 (로그인 정보, ADLS, API 설정)
├── requirements.txt       # Python 패키지 목록
├── downloads/             # 다운로드된 엑셀 파일 (날짜별)
├── state/                 # 런타임 상태 (failed_dates.json — 자동 생성, gitignore)
└── logs/                  # 실행 로그
```

## 대법원 나의사건검색 (사건 진행현황 수집)

대법원 나의사건검색(`ssgo.scourt.go.kr`)에서 지정한 사건들의 진행현황을 수집해 fss-webapp으로 전송한다.
(`main.py` → `run_court_crawl()`, **`CRAWL_ONLY=court` 잡으로 별도 실행/별도 cron**)

**동작**
1. Key Vault에서 사건목록(`COURT-CASES`)과 Anthropic 키(`ANTHROPIC-API-KEY`) 로드 — 없으면 조용히 skip
2. 사건별: 법원/년도/사건구분/일련번호/당사자명 입력
3. 자동입력 방지문자(캡차, 6자리 숫자)를 **Anthropic vision으로 OCR** (실패 시 새로고침 후 재시도, 최대 `CAPTCHA_MAX_TRY`회)
4. 진행내용 탭 → **송달결과 '확인' 체크박스 ON**(이걸 켜야 결과 칸에 'O시 도달' 등 송달결과가 표시됨) → 일자별 진행사항/메타/기일 파싱
5. 사건별 스냅샷을 fss-webapp API로 전송. **변동감지·알람은 webapp 책임** (크롤러는 diff 안 함)

> 캡차 OCR은 LLM 호출 1회/시도. 사건 수 × 시도 횟수만큼 Anthropic 비용 발생.

### 설정 위치 (사건검색용)

| 항목 | 권장 위치 | 이유 |
|---|---|---|
| Anthropic 키 | **VM `.env`의 `ANTHROPIC_API_KEY`** (또는 KV `ANTHROPIC-API-KEY`) | 다른 앱이 안 쓰는 정적 시크릿. ekape 계정처럼 KV에 둘 필요 없음 |
| 사건 목록 | **Key Vault `COURT-CASES`** | webapp 관리화면이 써넣고 크롤러가 읽는 **공유 지점**이라 KV 필수 |

- Anthropic 키 해석 순서: **env(`ANTHROPIC_API_KEY`) 우선 → 없으면 KV(`ANTHROPIC-API-KEY`)**. 그래서 `.env`에 넣으면 KV 시크릿을 안 만들어도 동작.
- 사건 목록 해석 순서: **KV(`COURT-CASES`) 우선 → 없으면 env(`COURT_CASES`)** (env는 주로 로컬 테스트용).

`COURT-CASES` 스키마 (배열 각 원소):
```json
[
  { "court": "대전고등법원", "caseNo": "2025나1073", "party": "덕원농장영농조합법인",
    "id": "case-001", "alias": "물품대금 항소심", "enabled": true }
]
```
- 필수: `court`(법원 전체명), `caseNo`(예: `2025나1073`), `party`(당사자명, 본인확인용)
- 선택: `id`(webapp CRUD 키), `alias`(메모), `enabled`(false면 수집 제외)
- `caseNo`는 크롤러가 `^(\d{4})(\D+)(\d+)$`로 년도/사건구분부호/일련번호로 분해

### webapp 전송 계약 (별도 세션에서 webapp 구현 시 참조)

- `POST {FSS_WEBAPP_API_URL}{COURT_PROGRESS_API_PATH}` (기본 `/api/court-case-progress`)
- 헤더: `x-api-key: {FSS_WEBAPP_API_KEY}`
- **사건 1건당 1회 POST.** 바디(스냅샷):
```json
{
  "job_name": "court-case-crawler",
  "case_id": "case-001",
  "court": "대전고등법원", "case_no": "2025나1073", "party": "덕원농장영농조합법인",
  "status": "success",
  "case_name": "[전자]물품대금", "court_dept": "제2민사부(가)...",
  "received_date": "2025-11-06", "final_result": "",
  "progress": [ { "date": "2025-11-10", "content": "...송달", "result": "2025.11.12 도달", "notice": "" } ],
  "hearings": [ { "date": "2026-07-09", "time": "15:30", "type": "변론기일", "location": "제304호 법정", "result": "" } ],
  "crawled_at": "2026-06-12T08:00:00+09:00",
  "error_message": null
}
```
- `status`: `success` | `not_found`(사건/당사자 불일치) | `captcha_failed` | `config_missing` | `error`
- **webapp 책임**: `case_no`(+`court`) 기준으로 직전 스냅샷과 `progress` 비교 → 신규 행 생기면 알람 발송. 크롤러는 매 실행 전체 스냅샷만 push.
- 당사자/대리인 이름은 사이트가 일부 마스킹(`주OOOO`)해서 내려줌 — 정상.

## 배포 (Azure Container Apps)

이미지 빌드/푸시는 ACR에서 직접 (로컬 Docker 불필요):
```bash
az acr build --registry fsswebappacr --image fss-crawler:latest --file Dockerfile .
```
잡(`fss-crawler-ekape`/`fss-crawler-court`)은 이미 생성돼 있고, **코드 변경 시 위 이미지만 재빌드하면 다음 실행부터 자동 반영**된다(잡 재생성 불필요).

### 로컬 개발/테스트
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium

CRAWL_ONLY=ekape python3 main.py    # 축평원만
CRAWL_ONLY=court python3 main.py    # 사건검색만
TARGET_DATE=2026-03-28 CRAWL_ONLY=ekape python3 main.py   # 특정 날짜 백필
```

## 환경변수 (.env)

```env
# 축산물원패스 로그인
EKAPE_ID=아이디
EKAPE_PW=비밀번호

# Azure ADLS 업로드
AZURE_STORAGE_CONNECTION_STRING=연결문자열
AZURE_STORAGE_CONTAINER=컨테이너명

# headless 모드 (VM/컨테이너 모두 true 필수)
CI=true

# Container Apps용 — Key Vault 조회 + 실행 분기/대상일
KEY_VAULT_URL=https://key-for-fssgradeuploader.vault.azure.net/
AZURE_CLIENT_ID=<id-fss-crawler clientId>   # user-assigned identity로 DefaultAzureCredential 인증
CRAWL_ONLY=all                # 잡별 지정: ekape | court | all(기본)
TARGET_DATE_CUTOFF_HOUR=12    # KST 이 시각 이전 실행=어제, 이후=오늘 조회 (기본 12)

# 대법원 나의사건검색
# Anthropic 키는 env 우선 → 없으면 KV(ANTHROPIC-API-KEY). VM은 아래 .env만으로 충분.
ANTHROPIC_API_KEY=sk-ant-...
# 사건 목록은 KV(COURT-CASES) 우선. 아래 COURT_CASES env는 KV 없을 때(로컬 테스트)만 사용.
#   COURT_CASES=[{"court":"대전고등법원","caseNo":"2025나1073","party":"덕원농장영농조합법인"}]
CAPTCHA_OCR_MODEL=claude-sonnet-4-6   # 캡차 OCR 모델 (기본값)
CAPTCHA_MAX_TRY=4                     # 캡차 재시도 횟수
COURT_PROGRESS_API_PATH=/api/court-case-progress   # webapp 전송 경로

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

## 운영 (Azure Container Apps)

### 수동 실행

```bash
az containerapp job start -n fss-crawler-ekape -g rg_dukwon   # 축평원
az containerapp job start -n fss-crawler-court -g rg_dukwon   # 사건검색
```
webapp 등에서 REST로 트리거: `POST .../Microsoft.App/jobs/{잡이름}/start?api-version=2024-03-01` (ARM 토큰 + 권한 `Container Apps Jobs Operator`).
특정 날짜 백필은 start 본문의 템플릿 오버라이드로 `TARGET_DATE` env를 덮어쓰면 된다(그 날짜 1회만, 자동 소급 스킵).

### 스케줄(cron) 확인/수정

```bash
# 확인
az containerapp job show -n fss-crawler-ekape -g rg_dukwon \
  --query "properties.configuration.scheduleTriggerConfig.cronExpression" -o tsv
# 수정 (cron은 UTC 기준! KST = UTC+9)
az containerapp job update -n fss-crawler-ekape -g rg_dukwon --cron-expression "0 10,14,23 * * *"
```
REST는 PATCH `.../jobs/{이름}` — `Microsoft.App/jobs/write`(=`Container Apps Jobs Contributor`) 권한 필요.

### 시크릿 (Key Vault 참조)

비밀 env는 Container Apps secret이 **Key Vault를 참조**(잡 정의에 평문 없음). KV 시크릿명 → env 매핑:

| Key Vault 시크릿 | 주입 env | 사용 잡 |
|---|---|---|
| `AZURE-STORAGE-CONNECTION-STRING` | `AZURE_STORAGE_CONNECTION_STRING` | ekape |
| `FSS-WEBAPP-API-KEY` | `FSS_WEBAPP_API_KEY` | ekape, court |
| `FSSWEBAPP-ALERTS-KEY` | `FSSWEBAPP_ALERTS_KEY` | ekape |
| `EKAPE-ID` / `EKAPE-PW` | (코드가 KV 직접 read) | ekape |
| `COURT-CASES` / `ANTHROPIC-API-KEY` | (코드가 KV 직접 read) | court |

KV 시크릿 추가/변경은 `Key Vault Secrets Officer` 권한 필요(포털 또는 `az keyvault secret set`).

## 실행 확인 (로그)

stdout은 Log Analytics(`ContainerAppConsoleLogs`)로 수집된다:
```bash
WS=$(az monitor log-analytics workspace list \
  --query "[?name=='workspace-rgdukwon9TDH'].customerId" -o tsv)
az monitor log-analytics query --workspace $WS --analytics-query \
  "ContainerAppConsoleLogs | where TimeGenerated > ago(1h) | where ContainerGroupName startswith 'fss-crawler' | project TimeGenerated, ContainerGroupName, Log | sort by TimeGenerated asc"
```
실행 이력/상태:
```bash
az containerapp job execution list -n fss-crawler-ekape -g rg_dukwon -o table
```
> 신규 워크스페이스는 첫 로그 인제스트가 5~10분 지연될 수 있다. 실행 성공/데이터 적재 여부는 fss-webapp 로그(등급/사건현황)로도 확인 가능.

## 문제 해결

### 로그인 타임아웃 (`Page.goto: Timeout ...`)
- 잡 단위 재시도(5분×2)가 자동으로 한 번 더 시도하므로 일시적 지연은 자체 복구됨. 로그에서 `[시도 n/3]` 확인.
- 계속 실패 시: Key Vault `EKAPE-ID`/`EKAPE-PW`(로컬은 `.env`) 확인, `curl -I https://www.ekape.or.kr`로 네트워크 확인.
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

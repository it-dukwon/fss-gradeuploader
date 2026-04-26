# VM 운영 가이드 (fss-gradeuploader)

> Azure VM(`fss-gradeuploader`)에서 cron으로 매일 도는 크롤러의 **운영/배포/장애대응** 절차.
> 코드/설계 설명은 `README.md` 참조. 이 문서는 **현장에서 SSH 들어가서 실행하는 명령**에 집중.

---

## 0. VM 접속

```bash
# Azure Portal → 가상 머신 → fss-gradeuploader → 연결 (SSH)
ssh dnftksdodi@<VM_PUBLIC_IP>
cd ~/fss-gradeuploader
```

VM은 **KST 07:55 자동 시작 → 08:00 cron → 08:30 자동 종료** (Azure Runbook).
SSH 작업이 필요하면 **운영 시간(07:55–08:30) 외에는 Portal에서 수동 시작** 후 종료 잊지 말 것.

---

## 1. 소스 업데이트 (git pull → 재배포)

```bash
cd ~/fss-gradeuploader

# 1) 코드 받기
git pull

# 2) (필요 시) 의존성 갱신
source venv/bin/activate
pip install -r requirements.txt

# 3) (코드 변경에 Playwright 영향 있을 때만)
playwright install --with-deps chromium

# 4) ⚠️ .env 갱신 필요 여부 확인 (아래 §1.1 참조)

# 5) 수동 실행으로 동작 확인
python3 main.py
```

### 1.1. ⚠️ 이번 배포(2026-04) 환경변수 마이그레이션

기존 VM `.env`에는 다음 키들이 있었음. **신규 코드는 이 키들을 더 이상 읽지 않으므로** 갱신 필수.
값은 **로컬 레포의 `.env`가 정본** — VM에는 §1.2 방식으로 통째로 복사.

**삭제 / 이름 변경**

| 구 키 | 신 키 |
|---|---|
| `AZURE_KEYVAULT_URL` | `KEY_VAULT_URL` |
| `FSS_WEBAPP_API_URL_DEV` / `_DEV2` / `_PRD` / `_PRD2` (4개) | `FSS_WEBAPP_API_URL` (1개) |
| `FSS_WEBAPP_API_KEY_DEV` / `_DEV2` / `_PRD` / `_PRD2` (4개) | `FSS_WEBAPP_API_KEY` (1개) |

**신규 추가**

- `FSSWEBAPP_ALERTS_URL`
- `FSSWEBAPP_ALERTS_KEY`

### 1.2. 로컬 `.env` → VM 으로 반영 (Azure Bastion)

**정본은 로컬 레포 `.env`**. VM에서 손으로 편집하지 말고, 아래 방법 중 택1로 통째 덮어쓰기.

> 어느 방법이든 시작 전 VM에서 백업 한 줄:
> `cp ~/fss-gradeuploader/.env ~/fss-gradeuploader/.env.bak.$(date +%Y%m%d) 2>/dev/null`

> **Bastion 클립보드 패널에 대해**: 브라우저가 클립보드 권한을 허용한 상태(처음 연결할 때 한 번 허용)면 패널 자체는 무시해도 됨. 로컬 `Ctrl+C` → Bastion 터미널 우클릭(또는 `Ctrl+Shift+V`)으로 바로 paste 가능. 패널은 권한이 막힌 환경의 fallback / 클립보드 내용 가시화 용도.

#### 방법 A. 복붙 (가장 간단, 1회성)

1. 로컬에서 `.env` 열어 **전체 복사** (`Ctrl+A` → `Ctrl+C`)
2. Portal → VM → **Bastion 연결** (브라우저 SSH)
3. 터미널에서:
   ```bash
   cat > ~/fss-gradeuploader/.env <<'EOF'
   ```
   Enter 직후 빈 줄에서 **우클릭(또는 `Ctrl+Shift+V`)으로 paste** → 마지막 줄에 `EOF` 한 줄 → Enter
4. 권한 정리:
   ```bash
   chmod 600 ~/fss-gradeuploader/.env
   ```

> `<<'EOF'`의 **따옴표** 중요 — 본문의 `$` 변수 확장 안 됨.

#### 방법 A-1. 한글 깨질 때 (base64 우회)

`.env`에 한글 주석이 있으면 클립보드 인코딩 문제로 깨질 수 있음 (PowerShell 5.1의 `Get-Content`가 CP949로 읽거나 Bastion 패널이 UTF-8 손실).
**base64로 감싸면** 모든 인코딩 우회 가능 (전송되는 건 ASCII만).

로컬 PowerShell:
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Users\dnftk\DEV\fss-gradeuploader\.env")) | Set-Clipboard
```

Bastion SSH 터미널:
```bash
cat > /tmp/env.b64 <<'B64_EOF'
```
→ Enter 후 paste → `B64_EOF` 입력 → Enter
```bash
base64 -d /tmp/env.b64 > ~/fss-gradeuploader/.env
chmod 600 ~/fss-gradeuploader/.env
rm /tmp/env.b64
```

#### 방법 B. Bastion 브라우저 파일 업로드 (Standard SKU)

Bastion 연결 패널의 **파일 업로드** → 로컬 `.env` 선택 → SSH 세션에서:
```bash
mv ~/.env ~/fss-gradeuploader/.env && chmod 600 ~/fss-gradeuploader/.env
```
> Basic SKU는 미지원 — 방법 A 또는 C 사용.

#### 방법 C. `az` 터널 + `scp` (반복 작업/스크립트화)

전제: 로컬에 Azure CLI + `az login`.

```bash
RG="<리소스 그룹>"  BASTION="<bastion 이름>"  VM_NAME="fss-gradeuploader"  SSH_USER="dnftksdodi"
VM_ID=$(az vm show -g "$RG" -n "$VM_NAME" --query id -o tsv)

# 터널 (이 터미널 유지, Ctrl+C로 종료)
az network bastion tunnel --name "$BASTION" --resource-group "$RG" \
  --target-resource-id "$VM_ID" --resource-port 22 --port 50022
```
별도 터미널에서:
```bash
scp -P 50022 ./.env "$SSH_USER@127.0.0.1:~/fss-gradeuploader/.env"
ssh -p 50022 "$SSH_USER@127.0.0.1" "chmod 600 ~/fss-gradeuploader/.env"
```
> host key 경고 나면 `~/.ssh/known_hosts`의 `[127.0.0.1]:50022` 줄 삭제 후 재시도.

### 1.3. 갱신 검증

#### 파일 자체 점검

```bash
# 1) 전체 내용 (시크릿 노출 주의)
cat ~/fss-gradeuploader/.env

# 2) 줄 수 / 바이트 수 (로컬 .env 와 비교)
wc -lc ~/fss-gradeuploader/.env

# 3) 인코딩 / 줄바꿈 확인 — "UTF-8 Unicode text" 만 떠야 정상
file ~/fss-gradeuploader/.env

# 4) CRLF 섞여 있으면(Windows 줄바꿈) LF 로 정리
sed -i 's/\r$//' ~/fss-gradeuploader/.env

# 5) 핵심 키 존재 확인 — 5줄 모두 출력되어야 함
grep -E '^(KEY_VAULT_URL|FSS_WEBAPP_API_URL|FSS_WEBAPP_API_KEY|FSSWEBAPP_ALERTS_URL|FSSWEBAPP_ALERTS_KEY)=' ~/fss-gradeuploader/.env

# 6) 구 키 잔존 확인 — 출력 없으면 OK
grep -E '^(AZURE_KEYVAULT_URL|FSS_WEBAPP_API_URL_(DEV|PRD)|FSS_WEBAPP_API_KEY_(DEV|PRD))' ~/fss-gradeuploader/.env
```

`file` 결과 해석:
- ✅ `UTF-8 Unicode text` — 정상
- ⚠️ `... with CRLF line terminators` — 위 4번 `sed` 한 번 돌리기
- ❌ `ISO-8859 text` 등 — 인코딩 깨짐, **§1.2 방법 A-1(base64)로 재업로드**

#### 실행 동작 점검

```bash
# 수동 실행 (KV/webapp 호출까지 한 사이클)
source venv/bin/activate && python3 main.py

# 최신 run 로그
tail -100 "$(ls -t ~/fss-gradeuploader/logs/run_*.log | head -1)"
```

로그에 다음 라인이 나오면 정상:
- `계정정보 로드: Key Vault (https://key-for-fssgradeuploader..., EKAPE-ID/EKAPE-PW)`
- `로그 전송 완료: ...`
- (실패 케이스일 때만) `[alerts] 호출 완료: ...`

---

## 2. 환경변수 점검

```bash
# .env 내용 확인 (시크릿 노출 주의)
cat ~/fss-gradeuploader/.env

# 핵심 키만 빠르게 확인
grep -E '^(KEY_VAULT_URL|FSS_WEBAPP_API_URL|FSSWEBAPP_ALERTS_URL|AZURE_STORAGE_CONTAINER)=' ~/fss-gradeuploader/.env
```

현재 운영에 필요한 핵심 변수:
- `KEY_VAULT_URL` — EKAPE 계정 시크릿 보관소 (Managed Identity 인증)
- `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_CONTAINER` — ADLS 업로드 대상
- `FSS_WEBAPP_API_URL`, `FSS_WEBAPP_API_KEY` — 실행 로그 적재
- `FSSWEBAPP_ALERTS_URL`, `FSSWEBAPP_ALERTS_KEY` — 실패 알람 호출
- `CI=true` — Playwright headless

---

## 3. 수동 실행

```bash
cd ~/fss-gradeuploader
source venv/bin/activate

# 어제 날짜로 (cron과 동일)
python3 main.py

# 특정 날짜로 (백필)
TARGET_DATE=2026-04-20 python3 main.py
```

여러 날짜 한 번에 백필:
```bash
for d in 2026-04-20 2026-04-21 2026-04-22; do
  TARGET_DATE=$d python3 main.py
done
```

---

## 4. cron 확인/수정

```bash
# 현재 등록된 잡 확인
crontab -l

# 편집
crontab -e
```

현재 cron 라인:
```cron
0 23 * * * cd ~/fss-gradeuploader && /home/dnftksdodi/fss-gradeuploader/venv/bin/python3 main.py >> /home/dnftksdodi/fss-gradeuploader/logs/cron_$(date +\%Y\%m\%d).log 2>&1
```

> 23:00 UTC = 08:00 KST. VM은 UTC 기준이므로 KST 시간 ÷ 24h 환산.

cron 데몬 자체 상태:
```bash
systemctl status cron     # 동작 중인지
sudo journalctl -u cron --since today | tail -50   # cron 실행 이벤트 로그
```

---

## 5. 로그 확인

### 5.1. 빠른 진단 (오늘자)

```bash
# 오늘 cron 실행 로그
tail -100 ~/fss-gradeuploader/logs/cron_$(date +%Y%m%d).log

# 가장 최근 run 로그 (한 번 실행할 때마다 새 파일 생성)
ls -lt ~/fss-gradeuploader/logs/run_*.log | head -3
tail -100 "$(ls -t ~/fss-gradeuploader/logs/run_*.log | head -1)"
```

### 5.2. 실시간 모니터링 (수동 실행 중)

```bash
tail -f "$(ls -t ~/fss-gradeuploader/logs/run_*.log | head -1)"
```

### 5.3. 에러/실패 검색

```bash
# 최근 7일치 run 로그에서 실패 줄 추출
grep -E "ERROR|실패|로그인 실패|타임아웃" ~/fss-gradeuploader/logs/run_*.log | tail -30

# 알람 호출 결과만
grep -E "\[alerts\]" ~/fss-gradeuploader/logs/run_*.log | tail -20

# 특정 날짜만
grep -E "ERROR|실패" ~/fss-gradeuploader/logs/run_20260425_*.log
```

### 5.4. 로그 라인 핵심 패턴

| 패턴 | 의미 |
|---|---|
| `계정정보 로드: Key Vault` | KV에서 EKAPE-ID/PW 정상 조회 |
| `로그인 성공!` | ekape 로그인 성공 |
| `로그인 실패!` | ekape 로그인 실패 (비번 만료/사이트 변경 등) |
| `조회 결과가 0건입니다` | 정상 종료 (그날 데이터 없음) |
| `완료: N개 파일 ADLS 업로드 성공` | 정상 완료 |
| `로그 전송 완료` | webapp `/api/grade-upload-logs` 호출 성공 |
| `[alerts] 호출 완료` | webapp `/api/alerts/fire` 호출 성공 (실패 시에만 발생) |
| `[alerts] 호출 실패 (swallow)` | 알람 호출 실패. 메인 잡에는 영향 없음. webapp 점검 필요 |

### 5.5. 다운로드/업로드 결과 확인

```bash
# 오늘 다운로드된 엑셀
ls -la ~/fss-gradeuploader/downloads/$(date +%Y%m%d)/

# 어제 다운로드된 엑셀
ls -la ~/fss-gradeuploader/downloads/$(date -d 'yesterday' +%Y%m%d)/
```

ADLS 업로드 결과는 webapp 대시보드 또는 Azure Portal Storage Explorer에서 확인:
- 컨테이너: `xls-uploader`
- 경로 패턴: 업로드 로직 확인 후 보강 필요

---

## 6. webapp 측 결과 확인

| 확인 항목 | 위치 |
|---|---|
| 잡 실행 이력 | webapp 대시보드 grade_upload_logs 테이블 / 페이지 |
| 알람 적재 (실패 시) | webapp DB `alerts_log` (status='fired' / 'suppressed') |
| 메일 도착 | 덕원관리자/웹관리자 메일함 |

---

## 7. 장애 대응 시나리오

### 7.1. 비밀번호 만료 / 로그인 실패

**증상**: `로그 전송 완료 ... "status":"fail"` + `[alerts] 호출 완료` 발생, 메일 수신.
로그에 `로그인 실패!`.

**대응**:
1. ekape.or.kr 포털에서 비번 변경
2. Azure Key Vault 시크릿 갱신:
   - Portal → Key Vaults → `key-for-fssgradeuploader` → Secrets
   - `EKAPE-PW` → 새 버전 추가 (값에 새 비밀번호)
3. VM에서 수동 1회 검증:
   ```bash
   cd ~/fss-gradeuploader && source venv/bin/activate && python3 main.py
   ```
4. 누락 날짜 백필:
   ```bash
   for d in 2026-04-20 2026-04-21 2026-04-22; do
     TARGET_DATE=$d python3 main.py
   done
   ```

### 7.2. cron이 안 돈 것 같을 때

```bash
# cron 데몬 상태
systemctl status cron

# cron 실행 이벤트 (커널 단위)
sudo journalctl -u cron --since "2 days ago" | grep CRON

# 우리 잡이 만든 로그 파일이 존재하는지
ls ~/fss-gradeuploader/logs/cron_$(date +%Y%m%d).log
```

→ 로그 파일이 없으면 cron이 트리거되지 않은 것. 가능성:
- VM이 그 시간에 안 켜져 있었음 (Runbook 점검)
- crontab 비어있음 (`crontab -l`)
- cron 데몬 중지

### 7.3. Playwright 브라우저 문제

```bash
# 시스템 의존성 재설치
playwright install --with-deps chromium

# 메모리 부족 의심 시
free -h
```

### 7.4. webapp 호출 실패 (logs/alerts)

로그에 `로그 전송 실패` / `[alerts] 호출 실패`:
1. webapp이 살아있는지: `curl -I https://fss.dukwonfarm.com`
2. 인증 키 일치 확인 (VM `.env`의 `FSS_WEBAPP_API_KEY` / `FSSWEBAPP_ALERTS_KEY` ↔ webapp App Service env)
3. 메인 잡(다운/업로드)에는 영향 없음 — 호출만 swallow됨

### 7.5. ADLS 업로드 실패

로그에 `업로드 중 오류`:
1. `AZURE_STORAGE_CONNECTION_STRING` 유효성: 키 만료/회전 여부
2. 컨테이너 존재 확인: Storage Explorer

---

## 8. 정기 점검 체크리스트 (월 1회 권장)

- [ ] cron 정상 실행: 최근 30일 `cron_*.log` 누락일 0건
- [ ] webapp grade_upload_logs: status=fail 발생일 추적
- [ ] webapp alerts_log: 디바운스 동작 정상 (suppressed가 fired 직후 등장)
- [ ] Key Vault 시크릿 만료일 (`EKAPE-PW`) — ekape 정책 따라 90일 등
- [ ] VM 디스크 사용량: `df -h ~` (downloads/logs 누적)
- [ ] 오래된 다운로드 정리 (선택):
  ```bash
  find ~/fss-gradeuploader/downloads -type d -mtime +90 -exec rm -rf {} +
  find ~/fss-gradeuploader/logs -name 'run_*.log' -mtime +90 -delete
  ```

---

## 9. 빠른 참조 (Cheat Sheet)

```bash
# 한 번에: 최신 로그 확인
tail -100 "$(ls -t ~/fss-gradeuploader/logs/run_*.log | head -1)"

# 한 번에: 수동 실행
cd ~/fss-gradeuploader && source venv/bin/activate && python3 main.py

# 한 번에: 어제 날짜 백필
cd ~/fss-gradeuploader && source venv/bin/activate && \
  TARGET_DATE=$(date -d 'yesterday' +%Y-%m-%d) python3 main.py

# 한 번에: 코드 갱신
cd ~/fss-gradeuploader && git pull && \
  source venv/bin/activate && pip install -r requirements.txt
```

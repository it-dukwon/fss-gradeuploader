# fss-gradeuploader ↔ fss-webapp 연동 사양: 자동 업로드 실패 알람

> **목적**: Azure VM 위에서 cron으로 도는 `fss-gradeuploader`(축산물원패스 등급판정결과 크롤러)가 실패했을 때, `fss-webapp`이 즉시 메일로 운영자에게 알림을 보내기 위한 인터페이스 사양.
>
> **이 문서의 역할**: VM(크롤러) 측에서 이미 한 일과 앞으로 호출할 사양을 정확히 전달하고, webapp이 구현해야 할 엔드포인트/DB/메일 발송/디바운스 정책을 명세.

---

## 1. 전체 흐름

```
[Azure VM cron (매일)]
   └─ fss-gradeuploader (Python)
        ├─ ekape.or.kr 로그인 → 등급판정결과 다운로드 → ADLS 업로드
        ├─ POST /api/grade-upload-logs   (항상 호출, 성공/실패 모두)
        └─ POST /api/alerts/fire         (status == "fail" 일 때만 추가 호출)  ← 이번 추가분

[fss-webapp]
   ├─ /api/grade-upload-logs : grade_upload_logs 테이블에 적재 (기존)
   └─ /api/alerts/fire       : alerts_log 적재 + 디바운스 + 메일 발송 (신규 요청)
```

**설계 의도**:
- 잡 실행 이력 적재(`grade-upload-logs`)와 사람에게 즉시 알릴 의도(`alerts/fire`)는 책임이 다르므로 분리.
- `alerts/fire`는 generic하게 두어 향후 다른 잡/시스템도 같은 인프라 재사용 가능 (`alert_code`로 분기).
- 디바운스/메일 발송 로직은 webapp 책임 (VM은 단순히 발생을 신고).

---

## 2. VM(fss-gradeuploader) 측 진행 현황

### ✅ 이미 완료
1. **EKAPE 계정 Azure Key Vault 조회**
   - Key Vault: `key-for-fssgradeuploader`
   - 시크릿 이름: `EKAPE-ID`, `EKAPE-PW`
   - VM의 System-assigned Managed Identity로 인증 (Key Vault Secrets User 권한 부여 완료)
2. **의존성 추가**: `azure-identity`, `azure-keyvault-secrets`
3. **Azure 인프라**: VM MI On + Key Vault IAM 부여 완료

### ✅ 추가 완료 (이번 작업)
1. `notify_failure()` 함수 추가 → `main()` 실패 시 `/api/alerts/fire` 호출 (`main.py`)
2. VM `.env` 갱신: `FSSWEBAPP_ALERTS_URL`, `FSSWEBAPP_ALERTS_KEY` 추가
3. **운영 단일화**: 기존 `report_log`가 dev/prd 양쪽 4개 webapp으로 보내던 다중 타겟 패턴 제거. 이제 logs/alerts 모두 **prd 커스텀 도메인 1개**(`https://fss.dukwonfarm.com`)로만 호출. dev webapp은 사실상 미사용 환경이므로 호출 대상에서 제외.

> **참고**: alerts 키는 webapp에서만 검증 가능한 값이므로 Key Vault에 두지 않고 VM `.env`에 직접 보관.
> webapp App Service env `API_KEY_ALERTS` = `API_KEY_ALERTS-FOR-FSSGRADEUPLOADER` 값으로 등록됨.

---

## 3. WEBAPP 요청 사항

### 3.1. `POST /api/alerts/fire` 엔드포인트 신규 구현

**인증**: 헤더 `x-api-key: <API_KEY_ALERTS>`
- 키 미일치 → `401 Unauthorized`
- webapp 측에 키 미설정 시 → `500`

**요청 바디 (JSON)**:
```json
{
  "alert_code": "grade_upload_failed",
  "severity": "error",
  "source": "fss-gradeuploader",
  "occurred_at": "2026-04-25T22:05:13.512Z",
  "payload": {
    "job_name": "ekape-daily-crawl",
    "target_date": "2026-04-24",
    "error_message": "축산물원패스 로그인 실패",
    "started_at": "2026-04-25T07:00:00+09:00",
    "finished_at": "2026-04-25T07:05:13+09:00",
    "download_count": 0,
    "upload_count": 0
  }
}
```

**필드 사양**:
| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `alert_code` | string | ✅ | 알람 종류 식별자. 현재는 `"grade_upload_failed"` 하나. 향후 다른 종류 추가 가능. |
| `severity` | string | ✅ | `"info" \| "warn" \| "error"` |
| `source` | string | ✅ | 호출 시스템 식별자 (`"fss-gradeuploader"`) |
| `occurred_at` | ISO8601 string | ✅ | 발생 시각 (UTC 권장, VM은 UTC로 보냄) |
| `payload` | object | ✅ | `alert_code` 별 추가 컨텍스트. 자유 키 — webapp에서 JSONB로 그대로 보관 권장 |
| `payload.error_message` | string | — | 최대 500자 (VM에서 잘라 보냄) |

**응답**:
```json
// 200 OK
{ "ok": true, "status": "fired",      "alert_id": "..." }
{ "ok": true, "status": "suppressed", "alert_id": "..." }   // 디바운스 발동 시
```
- 비-200 응답이어도 VM은 swallow하므로 메인 잡에 영향 없음. 단, 응답이 빠르게 끝나도록 메일 발송은 비동기/큐로 처리 권장 (VM의 `requests` 타임아웃 10초).

### 3.2. DB 스키마 (제안)

```sql
CREATE TABLE alerts_log (
  id              BIGSERIAL PRIMARY KEY,
  alert_code      VARCHAR(64)  NOT NULL,
  severity        VARCHAR(16)  NOT NULL,
  source          VARCHAR(64)  NOT NULL,
  occurred_at     TIMESTAMPTZ  NOT NULL,
  payload         JSONB,
  status          VARCHAR(16)  NOT NULL,            -- 'fired' | 'suppressed'
  email_sent_at   TIMESTAMPTZ,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_alerts_log_dedup
  ON alerts_log (alert_code, source, occurred_at DESC);
```

> 스키마 형태는 webapp 팀 재량으로 조정 가능. 위 컬럼은 디바운스 쿼리(같은 `alert_code+source` 최근 30분)와 메일 발송 추적에 필요한 최소셋.

### 3.3. 디바운스 정책 (30분)

같은 `(alert_code, source)`로 최근 **30분 이내**에 `status='fired'`인 레코드가 존재하면:
- 새 요청은 `alerts_log`에 `status='suppressed'`로 적재
- 메일 발송 **하지 않음**
- 응답 `{ok: true, status: "suppressed"}`

윈도우는 일단 30분 고정. 향후 `alert_code`별 차등이 필요하면 코드별 설정 테이블로 확장.

### 3.4. 메일 발송

`status='fired'`인 경우만 발송:
- **수신자**: 덕원관리자, 웹관리자
  - 주소 관리 방식은 webapp 재량 (env var, config 테이블, 코드별 매핑 등). 결정 후 알려주세요.
- **제목 예**: `[FSS][error] grade_upload_failed — 2026-04-24 잡 실패`
- **본문**: `payload`의 핵심 필드(`target_date`, `error_message`, `started_at`, `finished_at`)를 깔끔히 표시. 가능하면 webapp의 grade-upload-logs 페이지 링크 동봉.
- 발송 후 `email_sent_at` 업데이트.
- 메일 발송 실패 시 **API 응답은 200 유지**(VM은 멱등성 신경 안 쓰는 발사 후 망각). webapp 내부 재시도 정책은 자유.

### 3.5. webapp 환경변수

```bash
# prd App Service 에 이미 등록됨 (값 확정)
API_KEY_ALERTS=API_KEY_ALERTS-FOR-FSSGRADEUPLOADER

# 메일 수신자 (관리 방식은 webapp 재량 — env / config 테이블 등)
ALERT_RECIPIENTS=admin@dukwonfarm.com,web@dukwonfarm.com   # 예시
```

**운영 정책 (확정)**:
- VM은 **prd webapp만 호출**: `https://fss.dukwonfarm.com/api/alerts/fire`
- dev webapp은 적재/운영 미사용이라 알람 대상 아님 → dev 측에 alerts 엔드포인트를 따로 만들 필요는 없음 (만들어도 무방하나 호출되지 않음)
- 기존 azurewebsites.net 도메인은 운영 표준에서 제외 (`fss.dukwonfarm.com` 단일 표준)

---

## 4. webapp 답변 부탁 (의사결정 항목)

| # | 항목 | 비고 |
|---|---|---|
| 1 | `alerts_log` 스키마 채택 / 수정안 | 위 3.2 기준으로 OK인지, 변경할지 |
| 2 | 메일 수신자 관리 방식 | env var? config 테이블? alert_code별 매핑? |
| 3 | 디바운스 윈도우 30분 고정 OK? | 향후 alert_code별 차등 필요 여부 |
| 4 | `partial` status도 알람 발송할지 | VM은 다운로드 N건 중 일부만 업로드 성공 시 `status="partial"` 로 webapp/grade-upload-logs에 마킹. 이 경우 alerts/fire는 **현재 안 보낼 예정**(error로만). 변경 의견 있으면 알려주세요. |
| 5 | `API_KEY_ALERTS` 값 | ✅ 결정 완료 — `API_KEY_ALERTS-FOR-FSSGRADEUPLOADER`. webapp prd App Service env에 등록됨, VM `.env`에도 동일 값 반영. |

---

## 5. VM 측 호출 코드 미리보기 (참고)

webapp 측이 받게 될 실제 요청 모양을 확인할 수 있게 첨부.

```python
def notify_failure(target_date, error_message, started_at, finished_at,
                   download_count, upload_count):
    url = os.getenv("FSSWEBAPP_ALERTS_URL", "").strip()
    key = os.getenv("FSSWEBAPP_ALERTS_KEY", "").strip()
    if not url:
        logger.warning("FSSWEBAPP_ALERTS_URL 미설정 — 알람 호출 skip")
        return

    payload = {
        "alert_code": "grade_upload_failed",
        "severity": "error",
        "source": "fss-gradeuploader",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "job_name": "ekape-daily-crawl",
            "target_date": target_date,
            "error_message": (str(error_message)[:500] if error_message else ""),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "download_count": download_count,
            "upload_count": upload_count,
        },
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"x-api-key": key} if key else {},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"[alerts] 호출 완료: {resp.json()}")
    except Exception as e:
        # 알람 호출 실패는 swallow — 메인 잡에 영향 X
        logger.warning(f"[alerts] 호출 실패 (swallow): {e}")


# main() 끝부분에서:
finished_at = datetime.now(KST)
report_log(target_date, status, download_count, upload_count,
           error_message, started_at, finished_at)

if status == "fail":
    notify_failure(target_date, error_message, started_at, finished_at,
                   download_count, upload_count)
```

---

## 6. 합동 검증 시나리오

webapp 구현 완료 + VM 코드 배포 후, **prd webapp** 대상으로 합동 확인 (dev webapp은 호출 대상 아님).

1. **정상 실행** — VM cron 실행 → 다운/업로드 성공 → `/api/grade-upload-logs`만 호출. `alerts/fire` 호출 **없음**. webapp 대시보드에 `status=success`.

2. **실패 시뮬** — Key Vault의 `EKAPE-PW`를 일부러 잘못된 값으로 변경 → VM 수동 실행(`python3 main.py`) → 로그인 실패 → `/api/grade-upload-logs` (status=fail) + `/api/alerts/fire` 둘 다 호출. webapp DB `alerts_log`에 `status='fired'` 1건 + 관리자 메일 도착.

3. **30분 내 재실행** — 2번 직후 동일하게 한 번 더 실행 → `alerts_log`에 `status='suppressed'` 추가 적재, 메일 미발송.

4. **메일 SMTP 장애 시뮬** — API 응답은 200으로 받되 webapp 로그에 발송 실패 기록. VM은 영향 없음.

검증 종료 후 `EKAPE-PW`는 정상 값으로 복원.

---

## 7. 향후 확장 메모 (지금은 X)

- 다른 알람 종류 추가 시 새 `alert_code` 정의 (예: `data_quality_anomaly`, `adls_upload_quota_warn`).
- 채널 다양화 시 webapp 측에서 `alert_code` → 채널(Slack/SMS 등) 라우팅 테이블 도입.
- VM에서 `severity="warn"` 또는 `"info"` 알람도 보내고 싶을 때를 위해 인터페이스는 미리 일반화.

---

**문의**: VM 측 변경 사항이나 인터페이스 협의 사항은 fss-gradeuploader 측에 회신 부탁드립니다.

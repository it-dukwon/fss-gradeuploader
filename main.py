"""
FSS Grade Uploader - Main orchestrator
1. Download grade results from ekape.or.kr
2. Upload Excel files directly to ADLS
3. Report execution log to fss-webapp API
4. (자동) 실패 누적 날짜를 state/failed_dates.json에서 읽어 소급 재시도
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import failed_dates

# Setup logging
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 잡 단위 재시도 (transient 실패 대응). 총 시도 = RETRY_MAX + 1.
# VM 가동창(KST 07:55~08:30) 안에 끝나도록 보수적으로 설정. env로 조정 가능.
RETRY_MAX = int(os.getenv("RETRY_MAX", "2"))                       # 재시도 횟수 (기본 2 → 총 3회 시도)
RETRY_INTERVAL_SEC = int(os.getenv("RETRY_INTERVAL_SEC", "300"))   # 재시도 간격 (기본 300s = 5분)


def report_log(target_date, status, download_count, upload_count, error_message, started_at, finished_at):
    """fss-webapp /api/grade-upload-logs 에 실행 로그 전송.

    재시도/간헐실패 정보는 별도 컬럼 없이 error_message에 실어 보낸다 (성공이어도 채울 수 있음).
    """
    api_url = os.getenv("FSS_WEBAPP_API_URL", "").rstrip("/")
    if not api_url:
        logger.warning("FSS_WEBAPP_API_URL 미설정 — 로그 전송 skip")
        return

    api_key = os.getenv("FSS_WEBAPP_API_KEY", "").strip()
    headers = {"x-api-key": api_key} if api_key else {}

    payload = {
        "job_name": "fss-gradeuploader",
        "target_date": target_date,
        "status": status,
        "download_count": download_count,
        "upload_count": upload_count,
        "error_message": error_message,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
    }

    full_url = f"{api_url}/api/grade-upload-logs"
    logger.info(f"[logs] 호출 시작: {full_url}")
    try:
        resp = requests.post(full_url, json=payload, headers=headers, timeout=10)
        body_preview = (resp.text or "")[:300]
        if resp.ok:
            logger.info(f"[logs] 호출 완료 HTTP {resp.status_code}: {body_preview}")
        else:
            logger.warning(f"[logs] 호출 실패 HTTP {resp.status_code}: {body_preview}")
    except Exception as e:
        logger.warning(f"[logs] 네트워크 오류 (swallow): {e}")


def notify_failure(target_date, error_message, started_at, finished_at, download_count, upload_count):
    """status=fail 시 fss-webapp /api/alerts/fire 호출 → 메일/디바운스는 webapp 책임.

    호출 실패는 swallow (메인 잡 영향 없음).
    """
    url = os.getenv("FSSWEBAPP_ALERTS_URL", "").strip()
    if not url:
        logger.warning("FSSWEBAPP_ALERTS_URL 미설정 — 알람 호출 skip")
        return

    key = os.getenv("FSSWEBAPP_ALERTS_KEY", "").strip()
    headers = {"x-api-key": key} if key else {}

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

    logger.info(f"[alerts] 호출 시작: {url}")
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        body_preview = (resp.text or "")[:300]
        logger.info(f"[alerts] 응답 HTTP {resp.status_code}: {body_preview}")

        # webapp agent 인계 사양에 따른 진단 힌트 (운영자 즉시 판별용)
        if resp.status_code == 401:
            logger.warning("[alerts] 401 → VM .env FSSWEBAPP_ALERTS_KEY 점검 필요")
        elif resp.status_code == 503:
            logger.warning("[alerts] 503 → webapp App Service env API_KEY_ALERTS 미등록 (fail-closed). webapp 운영자에 통보")
        elif resp.status_code == 502:
            logger.warning("[alerts] 502 → webapp 메일 발송 단계 실패 (DB는 적재됐을 수 있음). webapp 로그/SMTP 확인")
        elif resp.ok:
            try:
                data = resp.json()
                if data.get("status") == "suppressed":
                    logger.info(f"[alerts] 디바운스 적용: due_to={data.get('suppressed_due_to')}")
                elif data.get("status") == "sent":
                    logger.info(f"[alerts] 메일 발송됨: alert_id={data.get('alert_id')}, recipients={data.get('recipients_count')}")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[alerts] 네트워크 오류 (swallow): {e}")


def report_court_progress(snapshot):
    """사건 진행현황 스냅샷 1건을 fss-webapp로 전송.

    변동감지/알람은 webapp 책임. 여기서는 '현재 스냅샷'만 push한다.
    호출 실패는 swallow (다른 사건/메인 잡에 영향 없음).
    """
    api_url = os.getenv("FSS_WEBAPP_API_URL", "").rstrip("/")
    if not api_url:
        logger.warning("FSS_WEBAPP_API_URL 미설정 — 사건 진행현황 전송 skip")
        return False

    api_key = os.getenv("FSS_WEBAPP_API_KEY", "").strip()
    headers = {"x-api-key": api_key} if api_key else {}
    path = os.getenv("COURT_PROGRESS_API_PATH", "/api/court-case-progress")

    payload = {"job_name": "court-case-crawler", **snapshot}
    full_url = f"{api_url}{path}"
    try:
        resp = requests.post(full_url, json=payload, headers=headers, timeout=15)
        body_preview = (resp.text or "")[:200]
        if resp.ok:
            logger.info(f"[court] 전송 OK HTTP {resp.status_code} "
                        f"({snapshot.get('case_no')}): {body_preview}")
            return True
        logger.warning(f"[court] 전송 실패 HTTP {resp.status_code} "
                       f"({snapshot.get('case_no')}): {body_preview}")
    except Exception as e:
        logger.warning(f"[court] 네트워크 오류 (swallow): {e}")
    return False


def run_court_crawl():
    """대법원 나의사건검색 진행현황 수집 → 사건별 fss-webapp 전송.

    ekape 잡과 동일 실행/주기. 설정(COURT-CASES) 없으면 조용히 skip.
    """
    logger.info("=" * 60)
    logger.info("[Court] 대법원 나의사건검색 진행현황 수집")
    try:
        from court_case_search import run_crawl, STATUS_CONFIG_MISSING
    except Exception as e:
        logger.warning(f"[court] 모듈 로드 실패 — skip: {e}")
        return

    try:
        out = run_crawl()
    except Exception as e:
        logger.error(f"[court] 수집 중 예외: {e}")
        return

    if out["status"] == STATUS_CONFIG_MISSING:
        logger.info(f"[court] 미설정/설정오류 — skip: {out['error']}")
        return

    results = out.get("results", [])
    logger.info(f"[court] 수집 {len(results)}건 (overall={out['status']})")
    sent = 0
    for snap in results:
        if report_court_progress(snap):
            sent += 1
    logger.info(f"[court] webapp 전송 {sent}/{len(results)}건 완료")


def _run_once(target_date):
    """단일 target_date에 대해 다운로드+업로드를 1회 시도.

    보고/알람/상태파일 갱신은 하지 않는다 (호출자가 최종 결과로 1번만 처리).

    Returns:
        dict: {status, download_count, upload_count, error_message, retryable}
        - retryable: 일시적 실패라 재시도 의미가 있는지 (계정정보 누락 등은 False)
    """
    status = "success"
    download_count = 0
    upload_count = 0
    error_message = None
    retryable = False

    # Step 1: Download grade results from ekape
    logger.info("[Step 1] 축산물원패스 등급판정결과 다운로드")
    downloaded_files = []
    download_status = None
    download_error = None
    try:
        from download_grades import run_download, STATUS_OK, STATUS_NO_DATA, STATUS_CONFIG_MISSING
        result = run_download()
        downloaded_files = result["files"]
        download_status = result["status"]
        download_error = result["error"]
        download_count = len(downloaded_files)
    except Exception as e:
        logger.error(f"다운로드 중 오류: {e}")
        return {"status": "fail", "download_count": 0, "upload_count": 0,
                "error_message": str(e), "retryable": True}

    # 다운로드 단계 결과로 status 결정
    # - STATUS_OK: 파일 있음 → 업로드 진행
    # - STATUS_NO_DATA: 조회 결과 0건 → 정상 (success)로 기록하고 종료
    # - 그 외 (login_failed, config_missing, nav_failed, search_failed, error): 실패
    if download_status and download_status not in (STATUS_OK, STATUS_NO_DATA):
        status = "fail"
        error_message = download_error or f"다운로드 실패 ({download_status})"
        # 계정정보 누락은 재시도해도 동일 → 재시도 제외, 그 외(타임아웃/메뉴/조회 등)는 재시도 대상
        retryable = (download_status != STATUS_CONFIG_MISSING)

    if not downloaded_files:
        if download_status == STATUS_NO_DATA:
            logger.info("조회 결과가 0건입니다. 정상 종료합니다.")
        else:
            logger.warning(f"다운로드된 파일이 없습니다. (status={download_status}) 업로드를 건너뜁니다.")
        return {"status": status, "download_count": download_count, "upload_count": 0,
                "error_message": error_message, "retryable": retryable}

    logger.info(f"다운로드 완료: {len(downloaded_files)}개 파일")

    # Step 2: Upload to ADLS
    logger.info("[Step 2] ADLS에 업로드")
    try:
        from upload_grades import run_upload
        results = run_upload(downloaded_files)
        upload_count = len(results)
    except Exception as e:
        logger.error(f"업로드 중 오류: {e}")
        return {"status": "fail", "download_count": download_count, "upload_count": 0,
                "error_message": str(e), "retryable": True}

    # Summary
    logger.info("=" * 60)
    if results:
        logger.info(f"완료: {len(results)}개 파일 ADLS 업로드 성공")
        for r in results:
            logger.info(f"  {r['original']} -> {r['adls_name']}")
        if upload_count < download_count:
            # 일부만 업로드 — 재실행 시 중복 업로드 위험이 있어 재시도하지 않고 partial로 종료
            status = "partial"
            error_message = f"다운로드 {download_count}건 중 {upload_count}건만 업로드 성공"
    else:
        logger.warning("ADLS 업로드된 파일이 없습니다.")
        status = "fail"
        error_message = "업로드된 파일 없음"
        retryable = True
    logger.info("=" * 60)

    return {"status": status, "download_count": download_count, "upload_count": upload_count,
            "error_message": error_message, "retryable": retryable}


def process_target_date(target_date):
    """단일 target_date 처리: 5분 간격 재시도 + webapp 보고 + 상태파일 갱신.

    - _run_once()를 최대 (RETRY_MAX+1)회 시도 (transient 실패에 한해 RETRY_INTERVAL_SEC 간격).
    - 보고/알람/상태기록은 최종 결과로 1번만 수행 (재시도마다 메일 폭주 방지).
    - 성공이어도 failed_attempts>0이면 참고값으로 webapp에 함께 전송.
    """
    os.environ["TARGET_DATE"] = target_date

    started_at = datetime.now(KST)

    logger.info("=" * 60)
    logger.info(f"FSS Grade Uploader 실행 (target_date={target_date})")
    logger.info("=" * 60)

    attempts = 0
    failed_attempts = 0
    last_fail_error = None
    result = None
    for i in range(RETRY_MAX + 1):
        attempts += 1
        logger.info(f"[시도 {attempts}/{RETRY_MAX + 1}] target_date={target_date}")
        result = _run_once(target_date)

        if result["status"] != "fail":
            break  # success / partial → 종료

        failed_attempts += 1
        last_fail_error = result["error_message"]
        if not result["retryable"]:
            logger.info("재시도 불가 유형(예: 계정정보 누락) — 재시도 생략")
            break
        if i < RETRY_MAX:
            logger.warning(
                f"실패(retryable): {last_fail_error} "
                f"→ {RETRY_INTERVAL_SEC}초({RETRY_INTERVAL_SEC // 60}분) 후 재시도"
            )
            time.sleep(RETRY_INTERVAL_SEC)

    status = result["status"]
    download_count = result["download_count"]
    upload_count = result["upload_count"]
    error_message = result["error_message"]

    # webapp 컬럼 추가 없이 시도/재시도 정보를 error_message에 실어 보낸다.
    # - 최종 실패: 시도 횟수 표기
    # - 성공이어도 중간에 실패가 있었으면 '참고' 메모로 남겨 간헐 실패를 가시화
    if status == "fail":
        error_message = f"[{attempts}회 시도 모두 실패] {error_message}"
    elif failed_attempts:
        note = f"(참고) {attempts}회 시도 중 {failed_attempts}회 실패 후 성공. 마지막 실패: {last_fail_error}"
        error_message = f"{note} / {error_message}" if error_message else note
        logger.info(f"[요약] {note}")

    finished_at = datetime.now(KST)
    report_log(target_date, status, download_count, upload_count, error_message, started_at, finished_at)
    if status == "fail":
        notify_failure(target_date, error_message, started_at, finished_at, download_count, upload_count)
        failed_dates.record(target_date, error_message)
    else:
        failed_dates.clear(target_date)

    return status


def _resolve_crawl_only():
    """실행 대상을 결정: 'ekape' | 'court' | 'all'.

    우선순위: 명령행 `--only=ekape|court` > 환경변수 `CRAWL_ONLY` > 기본 'all'.
    (Container Apps Job은 잡별로 CRAWL_ONLY env를 다르게 주는 방식으로 분리한다.)
    """
    val = os.getenv("CRAWL_ONLY", "").strip().lower()
    for a in sys.argv[1:]:
        if a.startswith("--only="):
            val = a.split("=", 1)[1].strip().lower()
    return val if val in ("ekape", "court", "mainfarm") else "all"


def run_ekape(is_manual_run):
    """축평원 등급 다운로드/업로드 + 실패 소급 백필.

    is_manual_run(TARGET_DATE 수동지정)이면 해당 날짜만 처리하고 자동 백필은 스킵.
    """
    from download_grades import get_target_date_str
    today_target = get_target_date_str()

    process_target_date(today_target)

    if is_manual_run:
        logger.info("[main] TARGET_DATE 수동 지정 — 자동 백필 스킵")
        return

    pending = failed_dates.pending_for_retry(today_target)
    if not pending:
        logger.info("[main] 소급 재시도 대상 없음")
        return

    logger.info(f"[main] 소급 재시도 대상 {len(pending)}일: {pending}")
    for d in pending:
        try:
            process_target_date(d)
        except Exception as e:
            # 한 날짜 실패가 다음 날짜를 막지 않도록 swallow
            logger.error(f"[main] 백필 처리 중 예외 ({d}): {e}")

    os.environ.pop("TARGET_DATE", None)


def main():
    # 컨테이너 기동 가시성: Python 진입 즉시 stdout 출력 (기동 hang vs 앱 hang 구분용)
    print("[main] 컨테이너 진입 — main() 시작", flush=True)

    # .env 로드 전에 외부 주입 여부 먼저 캡처 (수동 백필 판별 — .env의 TARGET_DATE는 무시)
    is_manual_run = bool(os.environ.get("TARGET_DATE", "").strip())

    from dotenv import load_dotenv
    load_dotenv()

    only = _resolve_crawl_only()
    logger.info(f"[main] 실행 대상(CRAWL_ONLY): {only}")

    # 돼지도체(본장=덕원농장) 다운로드 — 독립 잡(download-only). ekape/court 와 배타적으로 실행.
    # 양식이 위임현황과 달라 엑셀 처리는 webapp 책임(요건: WEBAPP_mainfarm_excel_요건.md).
    if only == "mainfarm":
        try:
            from download_mainfarm import run_mainfarm_download
            out = run_mainfarm_download()
            files = out.get("files", [])
            logger.info(f"[mainfarm] 다운로드 status={out['status']} files={len(files)} "
                        f"기간={out.get('start')}~{out.get('end')}")
            if files:
                # mainfarm 전용 컨테이너로 업로드 (잡 env AZURE_STORAGE_CONTAINER=mainfarm-uploader).
                # prod xls-uploader(위임현황 양식)와 반드시 분리 — 양식이 다르므로.
                from upload_grades import run_upload
                results = run_upload(files)
                logger.info(f"[mainfarm] ADLS 업로드 {len(results)}/{len(files)}건")
        except Exception as e:
            logger.error(f"[main] 돼지도체(본장) 처리 중 예외 (swallow): {e}")
        return

    # 축평원 등급 다운로드/업로드 (+ 소급 백필)
    if only in ("all", "ekape"):
        run_ekape(is_manual_run)

    # 대법원 나의사건검색 진행현황 수집 (독립 실패 격리)
    if only in ("all", "court"):
        try:
            run_court_crawl()
        except Exception as e:
            logger.error(f"[main] 사건검색 수집 중 예외 (swallow): {e}")


if __name__ == "__main__":
    main()

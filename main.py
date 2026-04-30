"""
FSS Grade Uploader - Main orchestrator
1. Download grade results from ekape.or.kr
2. Upload Excel files directly to ADLS
3. Report execution log to fss-webapp API
4. (자동) 실패 누적 날짜를 state/failed_dates.json에서 읽어 소급 재시도
"""

import os
import sys
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


def report_log(target_date, status, download_count, upload_count, error_message, started_at, finished_at):
    """fss-webapp /api/grade-upload-logs 에 실행 로그 전송."""
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


def process_target_date(target_date):
    """단일 target_date에 대해 다운로드+업로드+webapp 보고+상태파일 갱신을 1회 수행.

    - target_date를 환경변수로 주입 (download_grades.get_target_date_str()이 읽음).
    - status=='fail'이면 failed_dates에 record, 그 외 (success/partial)는 clear.
    """
    os.environ["TARGET_DATE"] = target_date

    started_at = datetime.now(KST)

    logger.info("=" * 60)
    logger.info(f"FSS Grade Uploader 실행 (target_date={target_date})")
    logger.info("=" * 60)

    status = "success"
    download_count = 0
    upload_count = 0
    error_message = None

    # Step 1: Download grade results from ekape
    logger.info("[Step 1] 축산물원패스 등급판정결과 다운로드")
    downloaded_files = []
    download_status = None
    download_error = None
    try:
        from download_grades import run_download, STATUS_OK, STATUS_NO_DATA
        result = run_download()
        downloaded_files = result["files"]
        download_status = result["status"]
        download_error = result["error"]
        download_count = len(downloaded_files)
    except Exception as e:
        logger.error(f"다운로드 중 오류: {e}")
        status = "fail"
        error_message = str(e)

    # 다운로드 단계 결과로 status 결정
    # - STATUS_OK: 파일 있음 → 업로드 진행
    # - STATUS_NO_DATA: 조회 결과 0건 → 정상 (success)로 기록하고 종료
    # - 그 외 (login_failed, config_missing, nav_failed, search_failed, error): 실패로 기록하고 종료
    if download_status and download_status not in (STATUS_OK, STATUS_NO_DATA):
        status = "fail"
        error_message = download_error or f"다운로드 실패 ({download_status})"

    if not downloaded_files:
        if download_status == STATUS_NO_DATA:
            logger.info("조회 결과가 0건입니다. 정상 종료합니다.")
        else:
            logger.warning(f"다운로드된 파일이 없습니다. (status={download_status}) 업로드를 건너뜁니다.")
        finished_at = datetime.now(KST)
        report_log(target_date, status, download_count, upload_count, error_message, started_at, finished_at)
        if status == "fail":
            notify_failure(target_date, error_message, started_at, finished_at, download_count, upload_count)
            failed_dates.record(target_date, error_message)
        else:
            failed_dates.clear(target_date)
        return status

    logger.info(f"다운로드 완료: {len(downloaded_files)}개 파일")

    # Step 2: Upload to ADLS
    logger.info("[Step 2] ADLS에 업로드")
    try:
        from upload_grades import run_upload
        results = run_upload(downloaded_files)
        upload_count = len(results)
    except Exception as e:
        logger.error(f"업로드 중 오류: {e}")
        results = []
        status = "fail"
        error_message = str(e)

    # Summary
    logger.info("=" * 60)
    if results:
        logger.info(f"완료: {len(results)}개 파일 ADLS 업로드 성공")
        for r in results:
            logger.info(f"  {r['original']} -> {r['adls_name']}")
        if upload_count < download_count:
            status = "partial"
            error_message = f"다운로드 {download_count}건 중 {upload_count}건만 업로드 성공"
    else:
        logger.warning("ADLS 업로드된 파일이 없습니다.")
        if status != "fail":
            status = "fail"
            error_message = "업로드된 파일 없음"
    logger.info("=" * 60)

    finished_at = datetime.now(KST)
    report_log(target_date, status, download_count, upload_count, error_message, started_at, finished_at)
    if status == "fail":
        notify_failure(target_date, error_message, started_at, finished_at, download_count, upload_count)
        failed_dates.record(target_date, error_message)
    else:
        failed_dates.clear(target_date)

    return status


def main():
    # .env 로드 전에 외부 주입 여부 먼저 캡처 (수동 백필 판별 — .env의 TARGET_DATE는 무시)
    is_manual_run = bool(os.environ.get("TARGET_DATE", "").strip())

    from dotenv import load_dotenv
    load_dotenv()

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


if __name__ == "__main__":
    main()

"""
FSS Grade Uploader - Main orchestrator
1. Download grade results from ekape.or.kr
2. Upload Excel files directly to ADLS
3. Report execution log to fss-webapp API
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

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


API_TARGETS = [
    ("FSS_WEBAPP_API_URL", "FSS_WEBAPP_API_KEY"),           # public (기존)
    ("FSS_WEBAPP_API_URL_DEV", "FSS_WEBAPP_API_KEY_DEV"),   # dev
    ("FSS_WEBAPP_API_URL_PRD", "FSS_WEBAPP_API_KEY_PRD"),   # prd
]


def report_log(target_date, status, download_count, upload_count, error_message, started_at, finished_at):
    """fss-webapp API에 실행 로그를 전송 (설정된 모든 환경으로)"""
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

    sent = False
    for url_env, key_env in API_TARGETS:
        api_url = os.getenv(url_env, "").rstrip("/")
        if not api_url:
            continue

        headers = {}
        api_key = os.getenv(key_env, "")
        if api_key:
            headers["x-api-key"] = api_key

        try:
            resp = requests.post(f"{api_url}/api/grade-upload-logs", json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            logger.info(f"로그 전송 완료 [{url_env}]: {resp.json()}")
            sent = True
        except Exception as e:
            logger.warning(f"로그 전송 실패 [{url_env}] (메인 작업에는 영향 없음): {e}")

    if not sent:
        logger.warning("설정된 API URL이 없어 로그 전송을 건너뜁니다.")


def main():
    from dotenv import load_dotenv
    load_dotenv()

    started_at = datetime.now(KST)

    logger.info("=" * 60)
    logger.info("FSS Grade Uploader 시작")
    logger.info("=" * 60)

    status = "success"
    download_count = 0
    upload_count = 0
    error_message = None
    target_date = None

    # Step 1: Download grade results from ekape
    logger.info("[Step 1] 축산물원패스 등급판정결과 다운로드")
    try:
        from download_grades import run_download, get_target_date_str
        target_date = get_target_date_str()
        downloaded_files = run_download()
        download_count = len(downloaded_files)
    except Exception as e:
        logger.error(f"다운로드 중 오류: {e}")
        downloaded_files = []
        status = "fail"
        error_message = str(e)

    if not downloaded_files:
        logger.warning("다운로드된 파일이 없습니다. 업로드를 건너뜁니다.")
        finished_at = datetime.now(KST)
        report_log(target_date, status, download_count, upload_count, error_message, started_at, finished_at)
        return

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


if __name__ == "__main__":
    main()

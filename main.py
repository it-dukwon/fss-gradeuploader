"""
FSS Grade Uploader - Main orchestrator
1. Download grade results from ekape.or.kr
2. Upload Excel files directly to ADLS
"""

import os
import sys
import logging
from datetime import datetime
from pathlib import Path

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


def main():
    from dotenv import load_dotenv
    load_dotenv()

    logger.info("=" * 60)
    logger.info("FSS Grade Uploader 시작")
    logger.info("=" * 60)

    # Step 1: Download grade results from ekape
    logger.info("[Step 1] 축산물원패스 등급판정결과 다운로드")
    try:
        from download_grades import run_download
        downloaded_files = run_download()
    except Exception as e:
        logger.error(f"다운로드 중 오류: {e}")
        downloaded_files = []

    if not downloaded_files:
        logger.warning("다운로드된 파일이 없습니다. 업로드를 건너뜁니다.")
        return

    logger.info(f"다운로드 완료: {len(downloaded_files)}개 파일")

    # Step 2: Upload to ADLS
    logger.info("[Step 2] ADLS에 업로드")
    try:
        from upload_grades import run_upload
        results = run_upload(downloaded_files)
    except Exception as e:
        logger.error(f"업로드 중 오류: {e}")
        results = []

    # Summary
    logger.info("=" * 60)
    if results:
        logger.info(f"완료: {len(results)}개 파일 ADLS 업로드 성공")
        for r in results:
            logger.info(f"  {r['original']} -> {r['adls_name']}")
    else:
        logger.warning("ADLS 업로드된 파일이 없습니다.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

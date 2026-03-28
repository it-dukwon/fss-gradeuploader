"""
등급판정결과 엑셀 파일을 Azure Data Lake Storage(ADLS)에 직접 업로드
- fss-webapp과 동일한 Connection String 인증 방식
- 파일명 형식: YYYYMMDD_HHmmss_SSS.xls (서울 시간 기준)
"""

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from azure.storage.filedatalake import DataLakeServiceClient

logger = logging.getLogger(__name__)

# 서울 시간대 (UTC+9)
KST = timezone(timedelta(hours=9))


def upload_to_adls(file_paths):
    """다운로드된 엑셀 파일들을 ADLS에 업로드"""
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    container_name = os.getenv("AZURE_STORAGE_CONTAINER")

    if not connection_string:
        logger.error("AZURE_STORAGE_CONNECTION_STRING 환경변수가 설정되지 않았습니다.")
        return []
    if not container_name:
        logger.error("AZURE_STORAGE_CONTAINER 환경변수가 설정되지 않았습니다.")
        return []

    if not file_paths:
        logger.warning("업로드할 파일이 없습니다.")
        return []

    logger.info(f"=== ADLS 업로드 시작 ({len(file_paths)}개 파일) ===")

    # ADLS 클라이언트 초기화
    try:
        service_client = DataLakeServiceClient.from_connection_string(connection_string)
        file_system_client = service_client.get_file_system_client(container_name)

        if not file_system_client.exists():
            logger.error(f"ADLS 파일시스템 \'{container_name}\'이 존재하지 않습니다.")
            return []
    except Exception as e:
        logger.error(f"ADLS 연결 실패: {e}")
        return []

    uploaded_files = []

    for file_path in file_paths:
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                logger.warning(f"  파일이 존재하지 않습니다: {file_path}")
                continue

            # fss-webapp과 동일한 파일명 형식: YYYYMMDD_HHmmss_SSS + 확장자
            now = datetime.now(KST)
            timestamp = now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
            ext = file_path.suffix or ".xls"
            adls_filename = f"{timestamp}{ext}"

            # ADLS에 파일 업로드 (create -> append -> flush)
            file_client = file_system_client.get_file_client(adls_filename)
            file_client.create_file()

            with open(file_path, "rb") as f:
                file_content = f.read()

            file_client.append_data(file_content, offset=0, length=len(file_content))
            file_client.flush_data(len(file_content))

            uploaded_files.append({
                "original": file_path.name,
                "adls_name": adls_filename,
                "size": len(file_content),
            })
            logger.info(f"  -> 업로드 완료: {file_path.name} -> {adls_filename} ({len(file_content):,} bytes)")

            # 파일 간 딜레이 (타임스탬프 충돌 방지)
            time.sleep(0.1)

        except Exception as e:
            logger.error(f"  업로드 실패 ({file_path}): {e}")

    logger.info(f"총 {len(uploaded_files)}/{len(file_paths)}개 파일 ADLS 업로드 완료")
    return uploaded_files


def run_upload(file_paths):
    """메인 업로드 실행 함수"""
    return upload_to_adls(file_paths)

# Container Apps Job용 크롤러 이미지
# - playwright + chromium 의존성까지 한 번에 설치 (slim 베이스 + --with-deps)
# - 실행되면 main.py가 한 사이클(다운로드→업로드→사건검색→소급) 돌고 종료
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CI=true

WORKDIR /app

# 의존성 먼저 (레이어 캐시) → chromium + 시스템 라이브러리 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

COPY . .

CMD ["python", "main.py"]

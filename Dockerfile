# Container Apps Job용 크롤러 이미지
# - playwright + chromium 의존성까지 한 번에 설치 (slim 베이스 + --with-deps)
# - 실행되면 main.py가 한 사이클(다운로드→업로드→사건검색→소급) 돌고 종료
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CI=true

WORKDIR /app

# Xvfb 가상 디스플레이 — EKAPE(Nexacro) 는 headless 크로미움에서 조회 그리드가 렌더되지 않아
# headed 로 구동해야 한다. 컨테이너엔 디스플레이가 없으므로 xvfb 로 가상 디스플레이를 제공.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth \
 && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 (레이어 캐시) → chromium + 시스템 라이브러리 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

COPY . .

# xvfb-run 으로 가상 디스플레이 위에서 headed 크로미움 실행 (Nexacro 그리드 렌더 보장)
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", "python", "main.py"]

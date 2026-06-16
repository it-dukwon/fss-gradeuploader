# Container Apps Job용 크롤러 이미지
# - playwright + chromium 의존성까지 한 번에 설치 (slim 베이스 + --with-deps)
# - 실행되면 main.py가 한 사이클(다운로드→업로드→사건검색→소급) 돌고 종료
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CI=true \
    DISPLAY=:99

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

# 명시적 Xvfb(가상 디스플레이)를 백그라운드로 띄운 뒤 headed 크로미움 실행 (Nexacro 그리드 렌더 보장).
# xvfb-run 래퍼는 컨테이너에서 기동 hang 소지가 있어, 명시적 Xvfb + DISPLAY(=:99, ENV)로 구동한다.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 & sleep 1 && exec python -u main.py"]

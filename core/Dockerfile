# ai-manager Core 이미지. 빌드 컨텍스트는 저장소 루트다.
#   docker build -f core/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY ki ./ki
COPY toss ./toss
COPY core ./core

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e ./ki -e ./toss -e './core[push]'

# root로 실행하지 않는다. Compose는 배포 사용자 UID로 덮어써 호스트 데이터 소유자와 맞춘다.
RUN useradd --create-home --uid 10001 app
ENV HOME=/home/app
USER app

CMD ["core", "notify-worker", "--sync", "--interval", "5"]

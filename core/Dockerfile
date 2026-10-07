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

CMD ["core", "notify-worker", "--sync", "--interval", "5"]

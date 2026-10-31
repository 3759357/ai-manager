FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e .

RUN useradd --create-home --uid 10001 app
ENV HOME=/home/app
USER app

CMD ["ki", "--help"]

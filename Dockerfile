FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md /app/
COPY config /app/config
COPY src /app/src

RUN pip install --no-cache-dir .

CMD ["vpn-bot", "run"]


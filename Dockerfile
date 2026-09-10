FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --system trader && useradd --system --gid trader --uid 10001 trader
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY trader ./trader
COPY config ./config
RUN mkdir /app/data && chown trader:trader /app/data
USER 10001:10001
EXPOSE 8090
CMD ["python", "-m", "trader.main"]

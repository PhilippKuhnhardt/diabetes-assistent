FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 APP_PORT=8765 APP_DB=/data/app.sqlite3
WORKDIR /srv/app
COPY app ./app
COPY web ./web
RUN groupadd --system app && useradd --system --gid app --home /srv/app app \
    && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)" || exit 1
CMD ["python", "-m", "app.server"]


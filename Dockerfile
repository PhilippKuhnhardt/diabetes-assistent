# The calculation core is Rust (core/); Python serves the API and the page and calls it (app/core.py).
FROM rust:1.91-slim-bookworm AS core
WORKDIR /core
COPY core ./
RUN PYO3_NO_PYTHON=1 cargo build --release --locked -p diabetes-core-py

FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 APP_PORT=8765 APP_DB=/data/app.sqlite3
WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
COPY --from=core /core/target/release/libdiabetes_core.so ./app/diabetes_core.abi3.so
RUN groupadd --system app && useradd --system --gid app --home /srv/app app \
    && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)" || exit 1
CMD ["python", "-m", "app.server"]

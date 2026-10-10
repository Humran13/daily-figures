FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY webapp/ ./webapp/
COPY migrations/ ./migrations/
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh scripts/backup_db.sh scripts/daily-figures \
    && groupadd --gid "$APP_GID" dailyfigures \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home --shell /usr/sbin/nologin dailyfigures \
    && mkdir -p /app/data \
    && chown -R "$APP_UID:$APP_GID" /app

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3).status==200 else sys.exit(1)"

USER dailyfigures
CMD ["./docker-entrypoint.sh"]

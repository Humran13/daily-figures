FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY webapp/ ./webapp/
COPY migrations/ ./migrations/
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh scripts/backup_db.sh

RUN mkdir -p /app/data

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=3).status==200 else sys.exit(1)"

CMD ["./docker-entrypoint.sh"]

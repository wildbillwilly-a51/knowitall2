# The KnowItAll2 server: one memory shared by coding agents on several computers.
# Built from the KnowItAll2 folder (the one holding src/): see docs/server.md.
FROM python:3.12-slim

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KNOWITALL2_HOME=/data \
    KNOWITALL2_SERVER_HOST=0.0.0.0 \
    KNOWITALL2_SERVER_PORT=4191

# An unprivileged user that owns only the data folder.
RUN useradd --system --uid 10001 --no-create-home --home-dir /data --shell /usr/sbin/nologin knowitall2 \
    && mkdir /data \
    && chown knowitall2:knowitall2 /data

COPY src /app/src
# Fails the build if this Python's SQLite lacks full-text search, which KnowItAll2 needs.
RUN python -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')"

USER knowitall2
VOLUME /data
EXPOSE 4191
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('KNOWITALL2_SERVER_PORT', '4191') + '/api/v1/health', timeout=4)"]
ENTRYPOINT ["python", "-B", "-m", "knowitall2", "serve-http"]

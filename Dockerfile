FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# UID/GID of the unprivileged user running the bot. Match the host user that owns ./db
# so the database stays editable on the host (see README).
ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

# Fully pinned runtime dependencies only (no test/dev tools in the image).
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/db \
    && chown app:app /app/db

COPY . .

# Starts as root only to fix ownership of the mounted ./db, then drops to "app"
# and execs the command (the bot never runs as root). See docker_entrypoint.py.
ENTRYPOINT ["python", "docker_entrypoint.py"]
CMD ["python", "main.py"]

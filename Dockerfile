# Host-agnostic image: runs on Render, Railway, Fly, or any container runtime.
FROM python:3.12-slim

# PYTHONUNBUFFERED keeps the JSON log lines streaming rather than sitting in a buffer,
# which is what makes them visible in a host's live log view.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt itself changes, so
# an ordinary code change rebuilds in seconds rather than reinstalling everything.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Run as a non-root user. Nothing here needs root, and a container that does not have
# it cannot be talked into using it.
RUN useradd --create-home --uid 1000 clinic && chown -R clinic:clinic /app
USER clinic

EXPOSE 8000

# Shell form so ${PORT} expands: hosts assign the port at runtime rather than letting
# the image choose one.
#
# --workers 1 is deliberate, not a default left unexamined. Conversation state lives
# in memory (see app/state/store.py), so a second worker would be a second process
# with its own view of every conversation: a user could be asked for their email by
# one worker and have the reply land on another that has never heard of them. Raising
# this is safe only once the Redis backend exists.
#
# tzdata comes from the pip package in requirements.txt, so the image needs no system
# time zone database of its own.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1

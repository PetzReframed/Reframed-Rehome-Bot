# The bot uses only Python's standard library: the image is the interpreter and the package.
FROM python:3.15-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY rehome_bot/ rehome_bot/
# The game server's container runs as UID/GID 10001 and keeps its rehoming folders private to that user (folders
# 0700, files 0600), so the bot reads them as the same user. /state holds each pet's message ID.
RUN install -d -o 10001 -g 10001 /state
USER 10001:10001
VOLUME /state
CMD ["python", "-m", "rehome_bot"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home bot

# Mount a folder holding config.yaml and .env here; state, logs and reports land in it too.
WORKDIR /bot
USER bot
ENTRYPOINT ["forex-bot"]
CMD ["paper"]

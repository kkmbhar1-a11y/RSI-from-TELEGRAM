FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_listener.py .

# Mount a volume here in production so the session file and logs persist
# across container restarts.
RUN mkdir -p /app/logs

ENV LOG_DIR=/app/logs

CMD ["python", "telegram_listener.py"]

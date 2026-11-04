FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Client IP (email rate limit, webhook allowlist) comes from X-Forwarded-For only for proxies listed
# in FORWARDED_ALLOW_IPS (uvicorn reads this env; default 127.0.0.1). Compose sets it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

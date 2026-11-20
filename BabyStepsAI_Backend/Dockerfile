FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BABYSTEPS_AUTH_MODE=oidc \
    BABYSTEPS_DATA_ROOT=/data/private_users

WORKDIR /app
COPY BabyStepsAI_Backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY BabyStepsAI_Backend/ /app/
RUN mkdir -p /data/private_users /data/private_auth && chmod 0700 /data/private_users /data/private_auth

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]

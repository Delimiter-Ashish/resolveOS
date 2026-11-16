FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN grep -v pixeltable-pgserver requirements.txt > r.txt && pip install -r r.txt
COPY resolveos ./resolveos
COPY scripts ./scripts
RUN mkdir -p results && useradd -m app && chown -R app /app
USER app
EXPOSE 8080
# role: api | worker
ENV ROLE=api PORT=8080
CMD ["sh", "-c", "python -m resolveos.db init && if [ \"$ROLE\" = worker ]; then exec python -m resolveos.worker; else exec uvicorn resolveos.api.app:app --host 0.0.0.0 --port ${PORT} --workers ${API_WORKERS:-2} --proxy-headers; fi"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app/resource_pipeline

RUN mkdir -p /app/resource_pipeline/output \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8765

CMD ["python", "-m", "resource_pipeline.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]

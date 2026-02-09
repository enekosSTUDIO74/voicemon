FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY voicemon/ voicemon/
COPY alert_rules.yaml .

RUN pip install --no-cache-dir ".[all]"

CMD ["python", "-m", "voicemon.workers.processor"]

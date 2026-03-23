FROM python:3.12-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/* \
    && git config --global user.email "ai-workforce@local" \
    && git config --global user.name "AI Workforce"

WORKDIR /app

COPY pyproject.toml .
RUN pip install -e .

COPY src/ ./src/

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
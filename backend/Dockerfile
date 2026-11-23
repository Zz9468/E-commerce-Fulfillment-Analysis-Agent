FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.11.29 \
    && uv sync --locked --no-dev --python-preference only-system

COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

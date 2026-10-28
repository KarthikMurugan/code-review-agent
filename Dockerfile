FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 reviewer
WORKDIR /app
COPY pyproject.toml README.md requirements-server.lock ./
COPY src ./src
RUN pip install -r requirements-server.lock && pip install --no-deps . \
    && mkdir /data && chown reviewer:reviewer /data
USER reviewer
CMD ["uvicorn", "review_agent.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

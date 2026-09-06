FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir . && \
    useradd --create-home --uid 10001 phoneagent && \
    mkdir -p /app/artifacts && chown -R phoneagent:phoneagent /app/artifacts

USER phoneagent
EXPOSE 8788
CMD ["lobster-phone-agent"]

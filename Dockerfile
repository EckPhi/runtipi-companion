FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io sudo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
ARG APP_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${APP_VERSION}
RUN pip install --no-cache-dir .

ENV RUNTIPI_COMPANION_CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

CMD ["python", "-m", "runtipi_companion.container"]

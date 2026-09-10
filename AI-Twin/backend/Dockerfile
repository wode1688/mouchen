ARG PARAFORMER_ARCHIVE_NAME=sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2
ARG PARAFORMER_ARCHIVE_SHA256=9c49fd9c6fb63de8e18c1054cf3d100f804741b7e608e187923cd8ff09fa9f03
ARG PARAFORMER_ARCHIVE_SIZE=234051698
ARG PARAFORMER_MODEL_SHA256=f36a0433bcf096bd6d6f11b80a3ac8bed110bdca632fe0d731df8d1a84475945
ARG PARAFORMER_MODEL_SIZE=243371218
ARG PARAFORMER_VOCAB_SHA256=59aba8873a2ed1e122c25fee421e25f283b63290efbde85c1f01a853d83cb6e6

FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS stt-model

ARG PARAFORMER_ARCHIVE_NAME
ARG PARAFORMER_ARCHIVE_SHA256
ARG PARAFORMER_ARCHIVE_SIZE
ARG PARAFORMER_MODEL_SHA256
ARG PARAFORMER_MODEL_SIZE
ARG PARAFORMER_VOCAB_SHA256

RUN apt-get update \
    && apt-get install -y --no-install-recommends bzip2 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN curl --fail --location --connect-timeout 15 --max-time 900 --retry 3 --retry-all-errors \
        --output "${PARAFORMER_ARCHIVE_NAME}" \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${PARAFORMER_ARCHIVE_NAME}" \
    && test "$(stat --format='%s' "${PARAFORMER_ARCHIVE_NAME}")" = "${PARAFORMER_ARCHIVE_SIZE}" \
    && printf '%s  %s\n' "${PARAFORMER_ARCHIVE_SHA256}" "${PARAFORMER_ARCHIVE_NAME}" \
        | sha256sum --check --strict \
    && install -d -m 0755 /opt/mouchen-stt/models/paraformer \
    && tar --extract --bzip2 --file "${PARAFORMER_ARCHIVE_NAME}" \
        --directory /opt/mouchen-stt/models/paraformer --strip-components=1 \
    && test "$(stat --format='%s' /opt/mouchen-stt/models/paraformer/model.int8.onnx)" = "${PARAFORMER_MODEL_SIZE}" \
    && printf '%s  %s\n' "${PARAFORMER_MODEL_SHA256}" /opt/mouchen-stt/models/paraformer/model.int8.onnx \
        | sha256sum --check --strict \
    && printf '%s  %s\n' "${PARAFORMER_VOCAB_SHA256}" /opt/mouchen-stt/models/paraformer/tokens.txt \
        | sha256sum --check --strict \
    && find /opt/mouchen-stt/models/paraformer -mindepth 1 -maxdepth 1 -type f \
        ! -name 'model.int8.onnx' ! -name 'tokens.txt' ! -name 'README.md' -delete \
    && chmod 0444 /opt/mouchen-stt/models/paraformer/*

FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/tmp

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" mouchen \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin mouchen \
    && install -d -o "${APP_UID}" -g "${APP_GID}" -m 0700 /data

COPY --from=stt-model /opt/mouchen-stt /opt/mouchen-stt

COPY backend/requirements.txt backend/requirements-stt.txt /app/
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
        -r /app/requirements.txt -r /app/requirements-stt.txt

COPY --chown=${APP_UID}:${APP_GID} backend/app /app/app

USER ${APP_UID}:${APP_GID}
EXPOSE 8788
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=4 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/ready', timeout=2).read()"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8788", "--workers", "1", "--no-access-log", "--no-proxy-headers"]

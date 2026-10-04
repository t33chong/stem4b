FROM python:3.14-slim-bookworm AS builder
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src/ src/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.14-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 stem4b \
    && useradd --uid 10001 --gid stem4b --create-home stem4b \
    && mkdir /data && chown stem4b:stem4b /data
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels stem4b \
    && rm -rf /wheels
USER stem4b
WORKDIR /data
ENTRYPOINT ["stem4b"]
CMD ["--help"]

# syntax=docker/dockerfile:1.7-labs
FROM rust:1.89-alpine AS add-det
RUN apk add --no-cache musl-dev
RUN cargo install add-determinism --locked

FROM ghcr.io/astral-sh/uv:0.7-python3.13-alpine AS uv

FROM python:3.13-alpine AS builder
RUN apk update && apk add --no-cache build-base curl python3-dev nodejs

ARG SOURCE_DATE_EPOCH=0

ENV UV_LINK_MODE=copy
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_INSTALLER_METADATA=1

WORKDIR /app
RUN --mount=from=uv,source=/usr/local/bin/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=/app/uv.lock \
    --mount=type=bind,source=pyproject.toml,target=/app/pyproject.toml \
    --mount=type=bind,source=build.sh,target=/app/build.sh \
    --mount=type=bind,source=src,target=/app/src \
    --mount=type=bind,source=test,target=/app/test \
    ./build.sh

FROM python:3.13-alpine AS main
WORKDIR /app
COPY --link --from=builder /app /app
RUN --mount=from=add-det,source=/usr/local/cargo/bin/add-det,target=/bin/add-det \
    /bin/add-det -j 4 --handler=pyc .

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["migrate"]

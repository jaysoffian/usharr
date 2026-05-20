FROM alpine:edge AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_BINARY_PACKAGE=pymediainfo \
    UV_PYTHON_INSTALL_DIR=/usr/local/share/uv-python \
    UV_PYTHON_BIN_DIR=/usr/local/bin
WORKDIR /app
RUN uv python install 3.14
COPY pyproject.toml ./
COPY uv.lock* ./
RUN if [ -f uv.lock ]; then \
        uv sync --frozen --no-dev --no-install-project; \
    else \
        uv sync --no-dev --no-install-project; \
    fi

FROM jlesage/mediainfo:v26.05.1 AS mediainfo
RUN mkdir -p /out/usr/bin /out/usr/lib \
    && cp -a /usr/bin/mediainfo /out/usr/bin/ \
    && cp -a /usr/lib/libmediainfo.so* /out/usr/lib/ \
    && cp -a /usr/lib/libzen.so* /out/usr/lib/ \
    && cp -a /usr/lib/libtinyxml2.so* /out/usr/lib/

FROM alpine:edge
RUN apk add --no-cache ffmpeg dovi-tool
COPY --from=mediainfo /out/ /
WORKDIR /app
COPY --from=builder /usr/local /usr/local
COPY --from=builder /app/.venv /app/.venv
COPY usharr/ usharr/
RUN printf '#!/bin/sh\nexec python -m usharr.cli "$@"\n' > /usr/local/bin/usharr \
    && chmod +x /usr/local/bin/usharr
ENV PATH="/app/.venv/bin:$PATH" USHARR_CONFIG=/config/config.yaml
VOLUME ["/config"]
EXPOSE 8555
CMD ["uvicorn", "usharr.app:app", "--host", "0.0.0.0", "--port", "8555"]

ARG GIT_COMMIT=unknown
RUN printf '%s' "$GIT_COMMIT" > /app/VERSION
LABEL org.opencontainers.image.revision="$GIT_COMMIT" \
      org.opencontainers.image.title="usharr"

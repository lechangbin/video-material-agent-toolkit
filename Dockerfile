# syntax=docker/dockerfile:1.7

FROM python:3.14.6-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 \
        install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        fluxbox \
        fonts-liberation \
        fonts-noto-cjk \
        gnupg \
        novnc \
        tini \
        websockify \
        x11-utils \
        x11vnc \
        xvfb \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 \
        install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --home-dir /home/toolkit --uid 10001 toolkit \
    && install -d -o toolkit -g toolkit \
        /data \
        /data/cache \
        /data/config \
        /data/localappdata \
        /home/toolkit/.config \
        /home/toolkit/.config/fluxbox \
    && chown -R toolkit:toolkit /home/toolkit /data

WORKDIR /opt/toolkit
COPY packages/material-collector ./packages/material-collector
COPY packages/semvideo ./packages/semvideo
RUN find packages -type d -exec chmod 0755 {} + \
    && find packages -type f -exec chmod 0644 {} +

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venvs/material-collector \
    && /opt/venvs/material-collector/bin/python -m pip install \
        ./packages/material-collector \
    && python -m venv /opt/venvs/semvideo \
    && /opt/venvs/semvideo/bin/python -m pip install \
        ./packages/semvideo \
    && ln -s /opt/venvs/material-collector/bin/material-collector \
        /usr/local/bin/material-collector \
    && ln -s /opt/venvs/semvideo/bin/semvideo /usr/local/bin/semvideo

COPY skills ./skills
COPY LICENSE README.md SECURITY.md SOURCE_COMMITS.md ./
RUN find skills -type d -exec chmod 0755 {} + \
    && find skills -type f -exec chmod 0644 {} + \
    && chmod 0644 LICENSE README.md SECURITY.md SOURCE_COMMITS.md
COPY --chmod=0755 docker/toolkit-entrypoint.sh /usr/local/bin/toolkit-entrypoint

ENV DISPLAY=:99 \
    DISPLAY_WIDTH=1440 \
    DISPLAY_HEIGHT=900 \
    DISPLAY_DEPTH=24 \
    HOME=/home/toolkit \
    LOCALAPPDATA=/data/localappdata \
    XDG_CONFIG_HOME=/data/config \
    XDG_CACHE_HOME=/data/cache \
    HF_HOME=/data/cache/huggingface \
    PYTHONUNBUFFERED=1

USER toolkit
WORKDIR /data

EXPOSE 6080
VOLUME ["/data"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/toolkit-entrypoint"]
CMD ["desktop"]

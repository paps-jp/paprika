FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Chrome + Xvfb + x11vnc + websockify(noVNC) + ffmpeg(yt-dlp 用)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    xvfb x11vnc fluxbox \
    novnc websockify \
    fonts-noto-cjk \
    procps \
    ffmpeg \
    libglib2.0-0 \
    libgl1 \
    iptables \
 && mkdir -p /etc/apt/keyrings \
 && wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
     | gpg --dearmor -o /etc/apt/keyrings/google.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
     > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
# Worker-only: opencv (headless build, no GUI deps) for nodriver's
# verify_cf() -- template-matches the Cloudflare Turnstile checkbox
# in a screenshot so we can click it by coordinate (the checkbox
# lives in a cross-origin iframe / shadow DOM and can't be reached
# via the DOM). Hub / runner images don't need it, so it's not in
# the shared requirements.txt.
RUN pip install --no-cache-dir opencv-python-headless

COPY core /app/core
COPY server /app/server
COPY fetch_html.py /app/fetch_html.py

COPY docker/worker/entrypoint.sh /entrypoint.sh
COPY docker/worker/egress-firewall.sh /entrypoint-egress-firewall.sh
RUN chmod +x /entrypoint.sh /entrypoint-egress-firewall.sh

# Paprika-customised noVNC lite. Replaces the stock /usr/share/novnc/
# vnc_lite.html with one that includes a floating clipboard panel so
# operators can copy / paste text between their local machine and the
# remote Chrome without switching to the full vnc.html UI.
COPY docker/worker/vnc_lite.html /usr/share/novnc/vnc_lite.html

# Defaults — `docker run` won't need -e for these unless customizing
ENV HUB_URL=ws://paprika.lan:8000 \
    SLOT_POOL=2 \
    MAX_CONCURRENT=2 \
    LABELS="" \
    NOVNC_PUBLIC_HOST=auto \
    NOVNC_BASE_PORT=6080

# Auto-created volume for WORKER_ID persistence (so the same container/host
# always gets the same worker_id across `docker rm` + `docker run`).
VOLUME /root/.paprika

# Exposed inside the container; map to host with -p 6080-6081:6080-6081
# (or use --network host on Linux).
EXPOSE 6080 6081 9223 9224

CMD ["/entrypoint.sh"]

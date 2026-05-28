FROM python:3.13-slim

WORKDIR /app

# Install the Docker CLI client only (not the daemon). The hub uses it
# to spawn ephemeral paprika-runner containers via the mounted socket
# for codegen-loop jobs (RFC-002 follow-up PR-14b). Without this the
# hub cannot launch sandboxed scripts.
#
# We pull the static `docker` binary directly from download.docker.com
# rather than apt-installing docker.io. On Debian Trixie that package
# split its CLI out into a separate `docker-cli` (not yet available in
# the slim base) and the bare `docker.io` only ships docker-init. The
# static binary is one ~30MB file and avoids the dependency churn.
ARG DOCKER_CLI_VERSION=27.5.1
RUN set -eux \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl cifs-utils ffmpeg \
 && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
      -o /tmp/docker.tgz \
 && tar -xz -C /tmp -f /tmp/docker.tgz docker/docker \
 && mv /tmp/docker/docker /usr/local/bin/docker \
 && chmod +x /usr/local/bin/docker \
 && rm -rf /tmp/docker /tmp/docker.tgz \
 && apt-get remove -y curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Source
COPY core /app/core
COPY server /app/server
COPY fetch_html.py /app/fetch_html.py

ENV PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["python", "-m", "server", "--mode", "hub", "--port", "8000"]

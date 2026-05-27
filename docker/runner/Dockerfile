# paprika-runner -- ephemeral sandbox for LLM-generated paprika-client scripts.
#
# Spawned by the hub one container per attempt. Has no Chrome of its own;
# the script talks HTTP back to the hub's /sessions/* endpoints, which then
# drive an actual worker's Chrome.
#
# Hardening applied at `docker run` time by server/hub/runner.py:
#   --read-only --tmpfs /tmp:size=64m,exec
#   --memory 512m --cpus 1.0 --pids-limit 100
#   --network paprika_default     (can reach hub, not the open internet)
#   -e PAPRIKA_HUB=http://hub:8000
#
# Image stays minimal: python:3.11-slim + paprika-client + httpx.

FROM python:3.11-slim

# Non-root user so a successful in-script `os.system("...")` can't trivially
# rewrite system files. (Doesn't help against API abuse on the hub, but
# raises the bar against generic mischief.)
RUN useradd -r -u 1001 -d /home/runner -m runner

WORKDIR /tmp/install
COPY client/python /tmp/install/paprika-client
RUN pip install --no-cache-dir /tmp/install/paprika-client \
 && rm -rf /tmp/install

# Per-execution scratch directory mounted at runtime via --tmpfs.
WORKDIR /work

USER runner

# Code is piped over stdin (`python -c "$CODE"` substitute via `python -`).
# Operators can override CMD when invoking docker run.
ENTRYPOINT ["python"]
CMD ["-"]

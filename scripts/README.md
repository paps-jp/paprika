# scripts/

Operational helpers for managing a paprika fleet (one hub + N workers).

| Script | Purpose |
|---|---|
| `deploy.sh` | One-shot deployer: git pull → hub restart → worker fan-out → extra hosts。`REBUILD=1` で docker build を強制 |
| `sync-workers.sh` | rsync the local source tree to every worker, then restart its container. **No GitHub round-trip.** |
| `git-pull-workers.sh` | SSH into each worker and `git pull` the configured branch, then restart. Each worker hits GitHub. |
| `deploy-workers-from-api.sh` | Light alternative: pulls the worker list from `/workers/hosts` and rsyncs `server/`+`core/`+`VERSION` only. Use for code-only hotfixes; doesn't ship compose / Dockerfile / scripts changes. |
| `paprika-worker.sh` | Linux / macOS の Worker をローカルで `python -m server --mode worker` で起動する薄いラッパ |
| `*.env.example` | `extra-hosts.env` / `llm.env` の雛形 — copy to `*.env` で実値を設定 |

## Worker discovery

All deploy scripts now pull the worker list from the **hub's live
inventory** (`GET ${HUB_URL}/workers/hosts`, default `http://localhost:8000`).
A worker host added to the fleet shows up automatically; one removed
drops off. **No `workers.env` to drift out of sync.**

Manual override (CI, partial deploy, "this single host"):

```
WORKERS='root@host1 root@host2' ./scripts/sync-workers.sh
```

Knobs:
- `HUB_URL=http://localhost:8000` — where to ask for the worker list
- `SSH_USER=root` — used when building `WORKERS` from API responses
- `COMPOSE_FILE=docker-compose-worker.yml`
- `RESTART_SERVICE=worker`
- `REMOTE_PATH=/opt/paprika`

The deploy scripts assume **passwordless SSH** to each worker. Setup is
covered below.

## Picking a deploy method

| Situation | Use |
|---|---|
| Iterating fast on Python (agent_runner.py, prompts, hub admin UI) | `sync-workers.sh` |
| You want every worker to match a specific published commit | `git-pull-workers.sh` |
| Dockerfile or requirements.txt changed | either, but with `REBUILD=1 ./scripts/git-pull-workers.sh` (or pull + manual `docker compose -f docker-compose-worker.yml up -d --build` on each worker) |

Python edits land via the bind mount in `docker-compose-worker.yml`, so
a plain `restart` is enough -- only Dockerfile / requirements / system
deps need a full rebuild.

## SSH key setup (hub CT -> worker CTs)

The deploy scripts run from the hub CT (or any host with the source on
disk) and SSH into each worker as `root`. Passwordless key auth is the
sane default.

### 1. Generate a key on the hub CT (one-time)

```bash
# On the hub CT
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ''
cat /root/.ssh/id_ed25519.pub
```

The empty passphrase (`-N ''`) is intentional for unattended scripts.
Re-use an existing key if you already have one; just `cat` its `.pub`.

### 2. Install the public key on every worker

#### Option A — via the Proxmox host (recommended)

Works even when the worker has password SSH disabled (Debian 12 default
`PermitRootLogin prohibit-password`). On the Proxmox host:

```bash
# Save the hub's public key somewhere readable on the Proxmox host:
HUB_PUB=/root/hub.pub
scp root@<hub-CT-IP>:/root/.ssh/id_ed25519.pub "$HUB_PUB"

# For each worker CT (set CTIDs as appropriate):
for ctid in 201 202 203; do
  pct exec  "$ctid" -- mkdir -p /root/.ssh
  pct exec  "$ctid" -- chmod 700 /root/.ssh
  pct push  "$ctid" "$HUB_PUB" /root/.ssh/authorized_keys
  pct exec  "$ctid" -- chmod 600 /root/.ssh/authorized_keys
done
```

If a worker already has an `authorized_keys` file, append instead of
overwrite:
```bash
pct exec "$ctid" -- sh -c 'cat >> /root/.ssh/authorized_keys' < "$HUB_PUB"
```

#### Option B — via `ssh-copy-id` (if password auth works)

If you've already enabled `PermitRootLogin yes` and know the worker's
root password:

```bash
# On the hub CT
ssh-copy-id -i /root/.ssh/id_ed25519.pub root@<worker-host>
# (enters worker root password once, then key is installed)
```

### 3. Test

From the hub CT:

```bash
ssh root@<worker-host> 'hostname && date'
```

No password prompt = ready. If you get prompted, the public key isn't
in `/root/.ssh/authorized_keys` on that worker yet, or the perms are
wrong (must be 700 on `~/.ssh` and 600 on `authorized_keys`).

### 4. (Optional) Use SSH aliases

In `/root/.ssh/config` on the hub CT:

```
Host worker-tokyo
    HostName <worker-host>
    User root

Host worker-osaka
    HostName <worker-host>
    User root
```

Then `WORKERS="worker-tokyo worker-osaka" ./scripts/sync-workers.sh`
reads nicely without bare IPs. (Auto-discovery still returns IPs;
SSH aliases only help for manual overrides.)

## Typical iterate loop (with bind mount + rsync)

```bash
# On the hub CT (or any dev host with the source tree)
vim server/worker/agent_runner.py
# tweak something

./scripts/sync-workers.sh
# rsyncs to every worker, restarts container, done in 5-10 s total
```

When you _also_ want the local hub to pick up the change:

```bash
docker compose restart hub
```

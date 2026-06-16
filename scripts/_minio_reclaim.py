"""MinIO orphan reclaimer (run inside a hub container).

Lists bucket job-prefixes in parallel (split by the first hex char of the
job-id -> 16x faster than one delimited scan), cross-checks against the live DB
job-ids, and reports/deletes the orphans (prefixes whose job row is gone).

DRY-RUN by default. ``EXECUTE=1`` deletes. Safety guards abort the destructive
path if the DB id read looks incomplete (empty, far below the summary total, or
if nearly every prefix would be an orphan) -- so a DB hiccup can't nuke live
data.
"""
import asyncio
import os
import time

import botocore.config
from server.hub import objstore
from server.hub.objstore import _s3cfg


def _long_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=_s3cfg("s3_endpoint", "PAPRIKA_S3_ENDPOINT") or None,
        aws_access_key_id=_s3cfg("s3_access_key", "PAPRIKA_S3_ACCESS_KEY") or None,
        aws_secret_access_key=_s3cfg("s3_secret_key", "PAPRIKA_S3_SECRET_KEY") or None,
        region_name=_s3cfg("s3_region", "PAPRIKA_S3_REGION", "us-east-1"),
        config=botocore.config.Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
            read_timeout=600,
            connect_timeout=30,
            max_pool_connections=64,
        ),
    )


def _list_one(client, base, char):
    out = []
    pag = client.get_paginator("list_objects_v2")
    for page in pag.paginate(Bucket=objstore._bucket(), Prefix=base + char, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            jid = cp["Prefix"][len(base):].rstrip("/")
            if jid:
                out.append(jid)
    return out


async def list_minio(client):
    prefix = objstore._prefix()
    base = (prefix + "/") if prefix else ""
    # .16's btrfs is degraded -> a single big delimited listing with a multi-page
    # continuation times out mid-stream. Split into 256 two-hex sub-prefixes so
    # each is ~one page (no continuation), run them parallel, and SKIP any that
    # still time out (those job-ids are just missed this pass -- re-runnable,
    # never over-deletes).
    hexd = "0123456789abcdef"
    subs = [a + b for a in hexd for b in hexd]
    sem = asyncio.Semaphore(32)
    done = [0]

    async def _sub(sp):
        async with sem:
            try:
                r = await asyncio.to_thread(_list_one, client, base, sp)
            except Exception as e:  # noqa: BLE001
                print(f"  list miss {sp}: {repr(e)[:40]}", flush=True)
                r = []
            done[0] += 1
            if done[0] % 32 == 0:
                print(f"  ...listed {done[0]}/256 sub-prefixes", flush=True)
            return r

    res = await asyncio.gather(*[_sub(sp) for sp in subs])
    return [j for r in res for j in r]


async def db_ids_and_total():
    import httpx

    ids: set = set()
    off = 0
    async with httpx.AsyncClient(timeout=60.0) as h:
        try:
            s = await h.get("http://127.0.0.1:8100/jobs/summary")
            total = (s.json() or {}).get("total")
        except Exception:
            total = None
        while True:
            r = await h.get("http://127.0.0.1:8100/jobs", params={"limit": 500, "offset": off})
            jobs = (r.json() or {}).get("jobs", [])
            if not jobs:
                break
            for j in jobs:
                jid = j.get("job_id")
                if jid:
                    ids.add(jid)
            off += 500
    return ids, total


async def main():
    execute = os.environ.get("EXECUTE") == "1"
    client = _long_client()

    t = time.time()
    minio_ids = await list_minio(client)
    print(f"minio prefixes: {len(minio_ids)} (listed {time.time()-t:.0f}s, parallel x16)", flush=True)

    db_ids, total = await db_ids_and_total()
    print(f"db job-ids: {len(db_ids)}  (summary total: {total})", flush=True)

    # --- safety guards (protect live data from an incomplete DB read) ---
    if not db_ids:
        print("ABORT: db_ids empty", flush=True); return
    if total and len(db_ids) < total - max(200, int(total * 0.01)):
        print(f"ABORT: db_ids {len(db_ids)} < summary total {total} (incomplete read)", flush=True); return

    orphans = [j for j in minio_ids if j not in db_ids]
    print(f"ORPHANS: {len(orphans)} / {len(minio_ids)} minio prefixes", flush=True)
    print("sample orphans:", orphans[:8], flush=True)
    if minio_ids and len(orphans) > 0.97 * len(minio_ids):
        print("ABORT: nearly every prefix looks orphan -- DB read suspect", flush=True); return

    if not execute:
        sample = orphans[:50]
        sb = 0
        for jid in sample:
            r = await objstore.delete_prefix(jid, dry_run=True)
            sb += int(r.get("bytes") or 0)
        avg = sb / max(1, len(sample))
        print(
            f"DRY-RUN. avg {int(avg/1024/1024)}MB/orphan => est reclaimable "
            f"~{int(avg*len(orphans)/(1024**3))} GiB across {len(orphans)} orphans",
            flush=True,
        )
        return

    print(f"EXECUTE: deleting {len(orphans)} orphan prefixes (concurrency 24)...", flush=True)
    sem = asyncio.Semaphore(24)
    freed = [0, 0, 0]  # prefixes, objects, bytes

    async def _one(jid):
        async with sem:
            try:
                r = await objstore.delete_prefix(jid)
                freed[0] += 1
                freed[1] += int(r.get("objects") or 0)
                freed[2] += int(r.get("bytes") or 0)
                if freed[0] % 500 == 0:
                    print(f"  ...{freed[0]}/{len(orphans)}  {freed[2]/(1024**3):.1f} GiB freed", flush=True)
            except Exception as e:  # noqa: BLE001
                print("  err", jid, repr(e)[:50], flush=True)

    await asyncio.gather(*[_one(j) for j in orphans])
    print(
        f"DONE: deleted {freed[0]} prefixes, {freed[1]} objects, "
        f"{freed[2]/(1024**3):.2f} GiB freed", flush=True
    )


if __name__ == "__main__":
    asyncio.run(main())

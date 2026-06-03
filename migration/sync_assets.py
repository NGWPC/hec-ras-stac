"""
Sync asset files from Dewberry source to the destination data root.

Missing-source logging:
    Items whose source prefixes return no objects on S3 (i.e. Dewberry never
    pushed the assets) are written to <working-dir>/sync_assets_missing.tsv.
    These are silent no-ops in `aws s3 sync` — this log makes them visible.

For each item in the local working dir (in destination layout, produced by
rewrite_hrefs.py), copy its source assets from Dewberry's S3 into the
per-item destination folder:

    Source:
      s3://fimc-data/dewberry-stac/<original_source_prefix>/source_models/<original_source_hash_dir>/<file>
      s3://fimc-data/dewberry-stac/<original_source_prefix>/stac_items/<original_source_hash_dir>/<non-json-file>

    Destination:
      <data-root>/hec-ras/<collection-id>/<item-id>/<file>

Both source sections (source_models/ and stac_items/) flatten into the same
destination dir. Confirmed by earlier analysis: no filename collisions
between the two sections within any hash.

Reads `original_source_prefix` and `original_source_hash_dir` from each
item's `properties` block (set by rewrite_hrefs.py). Run order is:
    sync_items.py → rewrite_hrefs.py → sync_assets.py

`--data-root` is an S3 URI: `s3://<bucket>` (production) or
`s3://<bucket>/<key-prefix>` (multi-tenant / test).

Subset semantics: subsetting happens upstream in sync_items.py (--subset N
caps to N hash dirs per prefix). sync_assets.py walks whatever is local, so
the subset propagates naturally.

Cross-account mode (--via-local):
    For cases where source-account creds can't write to dest-account buckets
    (and vice versa), stage each item through the laptop. The script
    downloads with SOURCE_AWS_* env credentials, uploads with DEST_AWS_* env
    credentials, and deletes the local staging dir after upload.

    Intended for SMALL-scale runs (subset validation). For full-scale
    migrations, prefer direct S3-to-S3 with creds that have both reads and
    writes — avoids piping TB through your laptop.

Usage:
    # Direct S3-to-S3 (default — single cred set in env or --source-profile/--dest-profile)
    python sync_assets.py --data-root s3://fimc-data/hv-fim-dev-data

    # Cross-account via laptop staging
    python sync_assets.py --data-root s3://fimc-data/test-hv-fim-dev-data --via-local

    python sync_assets.py ... --dry-run
"""

import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SOURCE_BUCKET = "fimc-data"
SOURCE_PREFIX = "dewberry-stac"
DEFAULT_WORKING_DIR = os.environ.get("WORKING_DIR", "~/ras-stac-migration")


def validate_s3_root(uri: str, flag: str) -> str:
    """Validate an S3 root URI: s3://bucket[/prefix], no trailing slash."""
    if not uri.startswith("s3://"):
        raise argparse.ArgumentTypeError(f"{flag} must start with 's3://' (got '{uri}')")
    if uri.endswith("/"):
        raise argparse.ArgumentTypeError(f"{flag} must not have a trailing slash (got '{uri}')")
    tail = uri[len("s3://"):]
    if not tail or tail.startswith("/"):
        raise argparse.ArgumentTypeError(f"{flag} must include a bucket after 's3://' (got '{uri}')")
    return uri


def _aws_env(role: str) -> dict:
    """
    Return an env dict for an aws CLI call.

    role="source" pulls SOURCE_AWS_* if set, else falls back to AWS_*.
    role="dest"   pulls DEST_AWS_*   if set, else falls back to AWS_*.
    """
    prefix = "SOURCE_" if role == "source" else "DEST_"
    env = os.environ.copy()
    has_role_creds = any(env.get(f"{prefix}AWS_{k}") for k in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY"))
    if not has_role_creds:
        return env
    for k in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "SESSION_TOKEN"):
        v = env.get(f"{prefix}AWS_{k}")
        if v is not None:
            env[f"AWS_{k}"] = v
        elif k == "SESSION_TOKEN":
            env.pop("AWS_SESSION_TOKEN", None)
    return env


def _is_destination_path(path: Path, items_dir: Path) -> bool:
    """Destination layout: items/<collection>/<item-id>/<item-id>.json (3 parts under items_dir)."""
    try:
        rel = path.relative_to(items_dir)
    except ValueError:
        return False
    return len(rel.parts) == 3 and path.parent.name == path.stem


def enumerate_items(items_dir: Path) -> list[tuple[Path, str, str, str, str]]:
    """
    Walk the items dir and return one row per destination-layout item:
        (json_path, collection_id, item_id, source_prefix, source_hash_dir)

    Items missing the provenance props are logged and skipped — they were
    either never run through rewrite_hrefs.py or are in some other state.
    """
    rows = []
    for path in sorted(items_dir.rglob("*.json")):
        if not _is_destination_path(path, items_dir):
            continue
        try:
            item = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.warning(f"Skipping unreadable {path}: {e}")
            continue
        rel = path.relative_to(items_dir)
        collection_id, item_id = rel.parts[0], rel.parts[1]
        props = item.get("properties") or {}
        src_prefix = props.get("original_source_prefix")
        src_hash_dir = props.get("original_source_hash_dir")
        if not src_prefix or not src_hash_dir:
            logger.warning(f"Skipping {rel} — missing original_source_prefix/hash_dir "
                           "(run rewrite_hrefs.py first)")
            continue
        rows.append((path, collection_id, item_id, src_prefix, src_hash_dir))
    return rows


def _run_sync(src: str, dst: str, profile: Optional[str], extra: list[str],
              role: str, dry_run: bool) -> tuple[int, int]:
    """Run aws s3 sync and return (returncode, files_transferred)."""
    cmd = ["aws", "s3", "sync", src, dst, "--no-progress"] + extra
    if profile:
        cmd += ["--profile", profile]
    if dry_run:
        logger.info(f"[DRY RUN] ({role}) {' '.join(cmd)}")
        return 0, 0
    result = subprocess.run(cmd, env=_aws_env(role), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout_normalized = result.stdout.replace("\r\n", "\n")
    transferred = stdout_normalized.count("\ncopy: ") + stdout_normalized.count("\nupload: ")
    if result.returncode != 0:
        logger.error(f"aws s3 sync failed (rc={result.returncode}):\n{result.stderr[:2000]}")
    return result.returncode, transferred


def sync_item_direct(src_prefix: str, src_hash_dir: str, collection_id: str, item_id: str,
                     data_root: str, source_profile: Optional[str], dest_profile: Optional[str],
                     dry_run: bool) -> tuple[int, int]:
    """Direct S3-to-S3 — single cred call that must read source AND write dest.

    Returns (returncode, total_files_transferred).
    """
    dst = f"{data_root}/hec-ras/{collection_id}/{item_id}/"
    profile = dest_profile or source_profile
    role = "dest" if dest_profile else "source"

    src_models = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/{src_prefix}/source_models/{src_hash_dir}/"
    rc, n1 = _run_sync(src_models, dst, profile, [], role, dry_run)
    if rc != 0:
        return rc, 0

    src_stac = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/{src_prefix}/stac_items/{src_hash_dir}/"
    rc, n2 = _run_sync(src_stac, dst, profile, ["--exclude", "*.json"], role, dry_run)
    return rc, n1 + n2


def sync_item_via_local(src_prefix: str, src_hash_dir: str, collection_id: str, item_id: str,
                        data_root: str, staging_root: Path, dry_run: bool) -> tuple[int, int]:
    """Cross-account — download with source creds, upload with dest creds, then delete local.

    Returns (returncode, total_files_transferred).
    """
    staging = staging_root / collection_id / item_id
    src_models = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/{src_prefix}/source_models/{src_hash_dir}/"
    src_stac = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/{src_prefix}/stac_items/{src_hash_dir}/"
    dst = f"{data_root}/hec-ras/{collection_id}/{item_id}/"

    if not dry_run:
        staging.mkdir(parents=True, exist_ok=True)

    rc, n1 = _run_sync(src_models, staging.as_posix() + "/", None, [], "source", dry_run)
    if rc != 0:
        return rc, 0
    rc, n2 = _run_sync(src_stac, staging.as_posix() + "/", None, ["--exclude", "*.json"], "source", dry_run)
    if rc != 0:
        return rc, 0

    rc, n3 = _run_sync(staging.as_posix() + "/", dst, None, [], "dest", dry_run)
    if rc != 0:
        return rc, 0

    if not dry_run and staging.exists():
        shutil.rmtree(staging)

    return 0, n1 + n2 + n3


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync source_models/ + stac_items/ assets per-item to dev data root")
    parser.add_argument(
        "--data-root",
        required=True,
        type=lambda v: validate_s3_root(v, "--data-root"),
        help="Destination data root S3 URI, e.g. s3://hv-fim-dev-data or s3://fimc-data/test-hv-fim-dev-data",
    )
    parser.add_argument("--working-dir", default=DEFAULT_WORKING_DIR,
                        help="Local working dir produced by rewrite_hrefs.py (items/<collection>/<item>/<item>.json)")
    parser.add_argument("--source-profile", default=None, help="AWS profile for source reads (direct mode only)")
    parser.add_argument("--dest-profile", default=None, help="AWS profile for destination writes (direct mode only)")
    parser.add_argument(
        "--via-local",
        action="store_true",
        help="Stage each item through a local dir. Use SOURCE_AWS_* env for reads, DEST_AWS_* for writes.",
    )
    parser.add_argument(
        "--staging-dir",
        default=os.path.join(os.environ.get("WORKING_DIR", "~/ras-stac-migration"), "staging"),
        help="Local staging dir for --via-local (default: <working-dir>/staging)",
    )
    parser.add_argument("--workers", type=int, default=32,
                        help="Parallel sync workers (default: 32; use 1 for sequential)")
    parser.add_argument("--failure-threshold", type=int, default=0,
                        help="Abort after N item failures (default: 0 = no limit)")
    parser.add_argument("--progress-log", default=None,
                        help="Path to live progress log file (default: <working-dir>/sync_assets_progress.log)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    working_dir = Path(args.working_dir).expanduser()
    items_dir = working_dir / "items"
    if not items_dir.exists():
        logger.error(f"Items dir not found: {items_dir}")
        return 1

    progress_log_path = Path(args.progress_log).expanduser() if args.progress_log else working_dir / "sync_assets_progress.log"
    file_handler = logging.FileHandler(progress_log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)

    logger.info(f"Source:      s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/")
    logger.info(f"Dest:        {args.data_root}/hec-ras/")
    logger.info(f"Walk:        {items_dir}")
    logger.info(f"Progress log: {progress_log_path}")
    if args.via_local:
        logger.info(f"Mode:        via-local (staging at {args.staging_dir})")
    else:
        logger.info(f"Mode:        direct S3-to-S3 (workers={args.workers}, failure-threshold={args.failure_threshold or 'none'})")

    rows = enumerate_items(items_dir)
    if not rows:
        logger.error(f"No destination-layout items found in {items_dir} — run rewrite_hrefs.py first")
        return 1

    logger.info(f"{len(rows)} items to sync")
    staging_root = Path(args.staging_dir).expanduser()
    missing_log = working_dir / "sync_assets_missing.tsv"
    failed_log = working_dir / "sync_assets_failed.tsv"
    missing: list[tuple[str, str, str, str]] = []
    failed: list[tuple[str, str, str, str]] = []
    completed = 0
    aborted = False

    if args.via_local:
        for _, collection_id, item_id, src_prefix, src_hash_dir in rows:
            rc, transferred = sync_item_via_local(src_prefix, src_hash_dir, collection_id, item_id,
                                                  args.data_root, staging_root, args.dry_run)
            if rc != 0:
                logger.error(f"Sync failed for {collection_id}/{item_id} (source {src_prefix}/{src_hash_dir})")
                return rc
            if not args.dry_run and transferred == 0:
                logger.warning(f"Silent skip: {collection_id}/{item_id} — 0 files transferred")
                missing.append((collection_id, item_id, src_prefix, src_hash_dir))
    else:
        def _sync_one(row: tuple) -> tuple[str, str, str, str, int, int]:
            _, collection_id, item_id, src_prefix, src_hash_dir = row
            rc, transferred = sync_item_direct(src_prefix, src_hash_dir, collection_id, item_id,
                                               args.data_root, args.source_profile, args.dest_profile,
                                               args.dry_run)
            return collection_id, item_id, src_prefix, src_hash_dir, rc, transferred

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_sync_one, row): row for row in rows}
            for future in as_completed(futures):
                collection_id, item_id, src_prefix, src_hash_dir, rc, transferred = future.result()
                completed += 1

                if rc != 0:
                    failed.append((collection_id, item_id, src_prefix, src_hash_dir))
                    logger.error(f"FAILED: {collection_id}/{item_id} (source {src_prefix}/{src_hash_dir})")
                    if args.failure_threshold and len(failed) >= args.failure_threshold:
                        logger.error(f"Failure threshold ({args.failure_threshold}) reached — aborting")
                        pool.shutdown(wait=True, cancel_futures=True)
                        aborted = True
                        break
                else:
                    if not args.dry_run and transferred == 0:
                        missing.append((collection_id, item_id, src_prefix, src_hash_dir))
                        logger.warning(f"Silent skip: {collection_id}/{item_id} — 0 files transferred")

                if completed % 500 == 0:
                    logger.info(f"Progress: {completed}/{len(rows)} completed, "
                                f"{len(failed)} failed, {len(missing)} missing")

    if missing:
        with missing_log.open("w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["collection", "item_id", "source_prefix", "source_hash_dir"])
            w.writerows(missing)
        logger.warning(f"{len(missing)} silent skips (0 files transferred) — written to {missing_log}")

    if failed:
        with failed_log.open("w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["collection", "item_id", "source_prefix", "source_hash_dir"])
            w.writerows(failed)
        logger.error(f"{len(failed)} items failed — written to {failed_log}")
        return 1

    if aborted:
        return 1

    logger.info(f"Asset sync complete. {completed}/{len(rows)} items, {len(missing)} missing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

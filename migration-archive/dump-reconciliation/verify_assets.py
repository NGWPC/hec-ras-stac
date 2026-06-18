"""Pre-flight: confirm Dewberry's S3 still has the assets our local items reference.

Reads drop_classification.tsv (produced by build_drop_list.py), pulls every
surviving item, reads one representative asset HREF per item, and issues an
S3 HEAD against the source bucket. Failures are logged to
verify_assets_failures.tsv. Exits 1 if any failures are found.

By default checks one asset per item (the first non-thumbnail). Use --full
to HEAD every asset on every surviving item — slower (~10× the calls) but
catches per-asset deletions.

Source prefetch:
    At startup, a single paginated listing of s3://fimc-data/dewberry-stac/
    collects every hash dir that exists under source_models/ and stac_items/
    for all three top-level prefixes (ebfe, mip_30, mip_70). Items whose
    hash dir is absent from this set are immediately flagged as missing without
    issuing a HEAD — eliminating per-item API calls for known-missing assets.

Usage:
  python3 verify_assets.py                                    # uses local AWS_* creds
  python3 verify_assets.py --source-profile <profile>         # different cred set
  python3 verify_assets.py --full                             # HEAD every asset
  python3 verify_assets.py --items items.txt                  # scope to rel_paths listed in file
  python3 verify_assets.py --full --log outputs/run.log       # write stdout+stderr to file
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, TextIO

try:
    import boto3
    import botocore.exceptions
except ImportError:
    sys.stderr.write("boto3 not installed. Run: pip install boto3\n")
    sys.exit(1)

# Match `s3://fim/<prefix>/<section>/<hash>/<relative>` plus the variant
# `https://fim.s3.amazonaws.com//<prefix>/<section>/<hash>/<relative>` that
# appears on thumbnails.
HREF_RE = re.compile(
    r"^(?:s3://fim/|https?://fim\.s3\.amazonaws\.com/+)"
    r"(?P<prefix>ebfe|mip_30|mip_70)/"
    r"(?P<section>source_models|stac_items)/"
    r"(?P<hash>[A-Za-z0-9_-]+)/"
    r"(?P<rel>.+)$"
)
SOURCE_BUCKET = "fimc-data"
SOURCE_PREFIX = "dewberry-stac"


def _href_to_key(href: str) -> Optional[str]:
    """Translate a Dewberry-source HREF into the actual fimc-data S3 key."""
    m = HREF_RE.match(href)
    if not m:
        return None
    return f"{SOURCE_PREFIX}/{m['prefix']}/{m['section']}/{m['hash']}/{m['rel']}"


def _load_env_file(env_path: Path) -> None:
    """Load export statements from a .env file into os.environ."""
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ[key.strip()] = val.strip().strip('"').strip("'")


def _build_s3_client(profile: Optional[str], env_file: Optional[Path] = None):
    """Build a boto3 S3 client.

    Priority: --source-profile > --env-file > SOURCE_AWS_* > AWS_* env vars.
    Credentials are passed explicitly to avoid ~/.aws/credentials overriding env vars.
    """
    if env_file and env_file.exists():
        _load_env_file(env_file)
    if profile:
        return boto3.Session(profile_name=profile).client("s3")
    src_key = os.environ.get("SOURCE_AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")
    src_secret = os.environ.get("SOURCE_AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    src_token = os.environ.get("SOURCE_AWS_SESSION_TOKEN") or os.environ.get("AWS_SESSION_TOKEN")
    if src_key and src_secret:
        return boto3.Session(
            aws_access_key_id=src_key,
            aws_secret_access_key=src_secret,
            aws_session_token=src_token,
        ).client("s3")
    return boto3.client("s3")


def _fetch_source_hash_dirs(s3, prefixes: list[str] = ("ebfe", "mip_30", "mip_70")) -> set[tuple[str, str]]:
    """Return a set of (top_level_prefix, hash_dir) pairs that exist on S3.

    Paginates with delimiter='/' — 6 listing calls total (one per section per
    top-level prefix), regardless of how many hash dirs exist.
    """
    paginator = s3.get_paginator("list_objects_v2")
    found: set[tuple[str, str]] = set()
    for top in prefixes:
        for section in ("source_models", "stac_items"):
            list_prefix = f"{SOURCE_PREFIX}/{top}/{section}/"
            for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=list_prefix, Delimiter="/"):
                for cp in page.get("CommonPrefixes") or []:
                    hash_dir = cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]
                    found.add((top, hash_dir))
    return found


def _head_one(s3, bucket: str, key: str, retries: int = 3) -> Optional[str]:
    """HEAD one object. Returns None on success, an error label on failure."""
    last_err = ""
    for attempt in range(retries):
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return None
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return "404"
            last_err = code or str(e)[:80]
            time.sleep(1 + attempt)
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:80]
            time.sleep(1 + attempt)
    return f"ERROR:{last_err}"


def _iter_surviving_items(classification_tsv: Path):
    """Yield (id, collection, rel_path) for items that survived classification (MIGRATE or KEEP).

    rel_path is relative to the migration working dir (matches build_drop_list.py's convention).
    """
    with classification_tsv.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            if row["decision"] in ("MIGRATE", "KEEP"):
                yield row["id"], row["collection"], row["rel_path"]


def _representative_keys(abs_path: Path, full: bool) -> list[str]:
    """Read the local item JSON and return S3 keys for one or all of its assets."""
    import json
    try:
        with abs_path.open() as f:
            item = json.load(f)
    except Exception:
        return []
    keys: list[str] = []
    for asset_key, v in (item.get("assets") or {}).items():
        if not isinstance(v, dict):
            continue
        href = v.get("href", "")
        s3_key = _href_to_key(href)
        if not s3_key:
            continue
        if not full and asset_key.lower() == "thumbnail":
            continue
        keys.append(s3_key)
        if not full and keys:
            break
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--classification", type=Path,
                        default=Path(__file__).parent / "outputs" / "drop_classification.tsv")
    parser.add_argument("--failures-log", type=Path,
                        default=Path(__file__).parent / "outputs" / "verify_assets_failures.tsv")
    parser.add_argument("--source-profile", default=None, help="AWS profile for fimc-data reads")
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--full", action="store_true",
                        help="HEAD every asset on every surviving item (default: 1 per item)")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N items (for smoke testing)")
    parser.add_argument("--working-dir", type=Path, default=Path(os.environ.get("WORKING_DIR", Path.home() / "ras-stac-migration")),
                        help="local working dir (rel_paths in the classification TSV are relative to this)")
    parser.add_argument("--items", type=Path, default=None,
                        help="Optional file of rel_paths (one per line) to restrict the check to a subset")
    parser.add_argument("--log", type=Path, default=None,
                        help="Write all stdout and stderr to this file instead of the terminal")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="Path to a .env file to load AWS credentials from (e.g. ../../.env)")
    args = parser.parse_args()
    args.working_dir = args.working_dir.expanduser().resolve()

    _log_fh: Optional[TextIO] = None
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        _log_fh = args.log.open("w", buffering=1)
        sys.stdout = _log_fh
        sys.stderr = _log_fh

    if not args.classification.exists():
        sys.stderr.write(f"error: {args.classification} not found; run build_drop_list.py first\n")
        return 1

    item_filter: Optional[set[str]] = None
    if args.items:
        if not args.items.exists():
            sys.stderr.write(f"error: --items file {args.items} not found\n")
            return 1
        item_filter = {line.strip() for line in args.items.read_text().splitlines() if line.strip()}
        print(f"==> Item filter loaded: {len(item_filter)} rel_paths from {args.items}")

    s3 = _build_s3_client(args.source_profile, args.env_file)
    _creds = s3._request_signer._credentials.get_frozen_credentials()
    print(f"    using key: {_creds.access_key[:12]}, token set: {bool(_creds.token)}, token prefix: {_creds.token[:20] if _creds.token else 'none'}")

    print(f"==> Prefetching source hash dirs from s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/")
    source_hash_dirs = _fetch_source_hash_dirs(s3)
    print(f"    {len(source_hash_dirs)} hash dirs found across ebfe/mip_30/mip_70")

    print(f"==> Loading surviving items from {args.classification}")
    survivors = list(_iter_surviving_items(args.classification))
    if item_filter is not None:
        survivors = [r for r in survivors if r[2] in item_filter]
        print(f"    filtered to {len(survivors)} items matching --items")
    if args.limit:
        survivors = survivors[: args.limit]
    print(f"    surviving items to check: {len(survivors)}")

    mode = "all assets" if args.full else "one asset per item"
    print(f"==> S3 HEAD sweep ({mode}, {args.threads} threads)")

    def check_item(rec):
        _iid, _coll, rel_path = rec
        keys = _representative_keys(args.working_dir / rel_path, args.full)
        if not keys:
            return rec, "NO_ASSET_HREF"
        # Short-circuit: if the hash dir is absent from both source_models/ and
        # stac_items/ for this prefix, the entire source dir is missing — no HEADs needed.
        # parts: dewberry-stac / <top> / <section> / <hash> / ...
        parts = keys[0].split("/")
        if len(parts) >= 4:
            top, hash_dir = parts[1], parts[3]
            if (top, hash_dir) not in source_hash_dirs:
                return rec, f"MISSING_SOURCE_DIR:{keys[0]}"
        for key in keys:
            err = _head_one(s3, SOURCE_BUCKET, key)
            if err is not None:
                return rec, f"{err}:{key}"
        return rec, None

    failures: list[tuple[tuple[str, str, str], str]] = []
    start = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futs = {pool.submit(check_item, rec): rec for rec in survivors}
        for fut in as_completed(futs):
            rec, err = fut.result()
            done += 1
            if err is not None:
                failures.append((rec, err))
            if done % 5000 == 0 or done == len(survivors):
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(survivors) - done) / rate if rate else 0
                print(f"    ...{done}/{len(survivors)} ({rate:.0f}/s, ETA {eta/60:.1f} min, failures so far: {len(failures)})")

    print()
    print(f"==> Summary")
    print(f"      checked:  {len(survivors)}")
    print(f"      passed:   {len(survivors) - len(failures)}")
    print(f"      failed:   {len(failures)}")

    if failures:
        with args.failures_log.open("w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["id", "collection", "rel_path", "error"])
            for (item_id, coll, rel_path), err in sorted(failures):
                w.writerow([item_id, coll, rel_path, err])
        print(f"\n    failures written to {args.failures_log}")
        print(f"    FAIL — {len(failures)} items have missing source assets")
        return 1

    print(f"\n    PASS — all surviving items resolved on S3")
    return 0


if __name__ == "__main__":
    sys.exit(main())

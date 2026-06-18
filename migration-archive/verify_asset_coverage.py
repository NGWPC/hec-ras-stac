"""
Verify HEC-RAS asset coverage on the destination S3 bucket.

The sync_assets logs cannot be trusted on their own to answer "are this item's
assets on the destination?" for two reasons:

  1. sync_assets_failed.tsv is overwritten each run (mode "w"). When the first
     run was killed near its failure threshold, its failures were lost from the
     TSV. They survive only in sync_assets_progress.log.

  2. sync_assets_missing.tsv ("0 files transferred") conflates two states:
     source prefix genuinely empty  vs.  destination already had the files
     (a benign skip on a re-run). The log cannot distinguish them.

The only authoritative source of truth is the destination bucket itself.

Three modes:

  Targeted (default): check the known suspects against the destination.
      * run-1 failures parsed from the progress log (the blind spot)
      * a stratified sample of missing.tsv (to settle the already-synced theory)
    Writes reconcile_results.tsv (PRESENT / ABSENT / ERROR per item).

  Full coverage (--full): definitive census, no sampling.
      expected = post-rewrite items dir (the set sync_assets walked) — this is
                 the authoritative expected list; no need to re-derive from the
                 drop/override/rewrite inputs.
      actual   = one bulk `s3 ls --recursive` of <data-root>/hec-ras/.
      gaps     = expected - actual, written to coverage_gaps.tsv.
    Must run where the post-rewrite items dir lives (the EC2).

  Triage gaps (--triage-gaps coverage_gaps.tsv): for each gap, read the item's
    source provenance and check the source bucket — classifies SOURCE_EMPTY
    (legitimately absent) vs REAL_GAP (source has data, needs sync).

Note: S3 keys may contain spaces (HEC-RAS filenames), so `aws s3 ls` output is
parsed with split(maxsplit=3) to keep keys intact.

Usage:
    python verify_asset_coverage.py --data-root s3://fimc-data/hv-fim-dev-data
    python verify_asset_coverage.py --data-root s3://... --full
    python verify_asset_coverage.py --triage-gaps coverage_gaps.tsv
"""

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_PROGRESS_LOG = SCRIPT_DIR / "sync_assets_progress.log"
DEFAULT_MISSING_TSV = SCRIPT_DIR / "sync_assets_missing.tsv"
DEFAULT_WORKING_DIR = os.environ.get("WORKING_DIR", "~/ras-stac-migration")
OUTPUT_TSV = SCRIPT_DIR / "reconcile_results.tsv"
GAPS_TSV = SCRIPT_DIR / "coverage_gaps.tsv"
TRIAGE_TSV = SCRIPT_DIR / "gaps_triaged.tsv"

SOURCE_BUCKET = "fimc-data"
SOURCE_PREFIX = "dewberry-stac"

FAILED_LINE = re.compile(r"FAILED: (?P<collection>[^/]+)/(?P<item>.+?) \(source (?P<prefix>[^/]+)/(?P<hash>\S+)\)")
RERUN_MARKER = "INFO - Source:"


def parse_run1_failures(progress_log: Path) -> list[tuple[str, str, str, str]]:
    """Extract FAILED items from the FIRST run only (lines before the 2nd 'Source:' marker).

    Returns rows of (collection, item_id, source_prefix, source_hash_dir).
    """
    lines = progress_log.read_text(encoding="utf-8", errors="replace").splitlines()
    source_markers = [i for i, ln in enumerate(lines) if RERUN_MARKER in ln]
    run1_end = source_markers[1] if len(source_markers) >= 2 else len(lines)

    rows = []
    for ln in lines[:run1_end]:
        m = FAILED_LINE.search(ln)
        if m:
            rows.append((m["collection"], m["item"], m["prefix"], m["hash"]))
    return rows


def load_missing(missing_tsv: Path) -> list[tuple[str, str, str, str]]:
    """Load missing.tsv rows: (collection, item_id, source_prefix, source_hash_dir)."""
    if not missing_tsv.exists():
        return []
    with missing_tsv.open(encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return [(r["collection"], r["item_id"], r["source_prefix"], r["source_hash_dir"]) for r in reader]


def stratified_sample(rows: list[tuple[str, str, str, str]], n: int) -> list[tuple[str, str, str, str]]:
    """Sample ~n rows spread evenly across source_prefix (index 2)."""
    by_prefix: dict[str, list] = defaultdict(list)
    for row in rows:
        by_prefix[row[2]].append(row)
    per_bucket = max(1, n // len(by_prefix)) if by_prefix else n
    sample = []
    for prefix, group in by_prefix.items():
        sample.extend(random.sample(group, min(per_bucket, len(group))))
    return sample


def count_dest_objects(data_root: str, collection: str, item_id: str, profile: Optional[str]) -> int:
    """List the item's destination folder and count objects. -1 on AWS error."""
    dst = f"{data_root}/hec-ras/{collection}/{item_id}/"
    cmd = ["aws", "s3", "ls", dst, "--recursive"]
    if profile:
        cmd += ["--profile", profile]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return -1
    return sum(1 for ln in result.stdout.splitlines() if ln.strip())


def reconcile(label: str, rows: list[tuple[str, str, str, str]], data_root: str,
              profile: Optional[str]) -> list[tuple[str, str, str, str, str, int]]:
    """Check each row against the destination. Returns rows + (verdict, object_count)."""
    results = []
    present = absent = errored = 0
    for collection, item_id, prefix, hash_dir in rows:
        count = count_dest_objects(data_root, collection, item_id, profile)
        if count < 0:
            verdict = "ERROR"
            errored += 1
        elif count > 0:
            verdict = "PRESENT"
            present += 1
        else:
            verdict = "ABSENT"
            absent += 1
        results.append((label, collection, item_id, f"{prefix}/{hash_dir}", verdict, count))
    print(f"  [{label}] {len(rows)} checked: {present} PRESENT, {absent} ABSENT, {errored} ERROR")
    return results


def enumerate_expected_items(items_dir: Path) -> set[tuple[str, str]]:
    """Walk the post-rewrite items dir for the expected (collection, item_id) set.

    Destination layout: items/<collection>/<item-id>/<item-id>.json. This is the
    same set sync_assets.py walked, so it's the authoritative expected list with
    no need to re-derive collection rules from drop/override/rewrite inputs.
    """
    expected = set()
    for path in items_dir.rglob("*.json"):
        rel = path.relative_to(items_dir)
        if len(rel.parts) == 3 and path.parent.name == path.stem:
            expected.add((rel.parts[0], rel.parts[1]))
    return expected


def list_actual_item_folders(data_root: str, profile: Optional[str]) -> set[tuple[str, str]]:
    """One bulk `s3 ls --recursive` of <data-root>/hec-ras/, reduced to the set of
    (collection, item_id) folders that contain at least one object.
    """
    prefix = f"{data_root}/hec-ras/"
    cmd = ["aws", "s3", "ls", prefix, "--recursive"]
    if profile:
        cmd += ["--profile", profile]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: bulk list failed:\n{result.stderr[:1000]}", file=sys.stderr)
        sys.exit(1)

    bucket_path = data_root[len("s3://"):]
    key_prefix = bucket_path.split("/", 1)[1] + "/hec-ras/" if "/" in bucket_path else "hec-ras/"

    actual = set()
    for line in result.stdout.splitlines():
        # `aws s3 ls --recursive` is "DATE TIME SIZE KEY"; only the first 3 fields
        # are space-free, so maxsplit=3 keeps space-containing keys intact.
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        key = parts[3]
        if not key.startswith(key_prefix):
            continue
        rel = key[len(key_prefix):].split("/")
        if len(rel) >= 3:
            actual.add((rel[0], rel[1]))
    return actual


def run_full_coverage(items_dir: Path, data_root: str, profile: Optional[str]) -> int:
    """Set-diff expected (post-rewrite items dir) against actual (bulk S3 list)."""
    print(f"Walking expected items: {items_dir}")
    expected = enumerate_expected_items(items_dir)
    print(f"  expected items: {len(expected)}")

    print(f"Listing destination: {data_root}/hec-ras/ (one bulk call)...")
    actual = list_actual_item_folders(data_root, profile)
    print(f"  item folders with >=1 object on dest: {len(actual)}")

    gaps = sorted(expected - actual)
    print(f"\nCoverage: {len(expected) - len(gaps)}/{len(expected)} present, {len(gaps)} gaps")

    with GAPS_TSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["collection", "item_id"])
        w.writerows(gaps)
    print(f"Wrote {len(gaps)} gap rows to {GAPS_TSV}")

    if gaps:
        print("\nGaps (first 20):")
        for collection, item_id in gaps[:20]:
            print(f"  {collection}/{item_id}")
        return 1
    print("\nFULL COVERAGE — every expected item has assets on the destination.")
    return 0


def read_item_provenance(items_dir: Path, collection: str, item_id: str) -> Optional[tuple[str, str]]:
    """Read (original_source_prefix, original_source_hash_dir) from a gap item's JSON."""
    item_json = items_dir / collection / item_id / f"{item_id}.json"
    if not item_json.exists():
        return None
    try:
        props = json.loads(item_json.read_text(encoding="utf-8")).get("properties") or {}
    except json.JSONDecodeError:
        return None
    prefix = props.get("original_source_prefix")
    hash_dir = props.get("original_source_hash_dir")
    return (prefix, hash_dir) if prefix and hash_dir else None


def count_source_objects(prefix: str, hash_dir: str, profile: Optional[str]) -> int:
    """Count source objects across source_models/ + stac_items/ (assets only). -1 on error."""
    base = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/{prefix}"
    total = 0
    for section in ("source_models", "stac_items"):
        cmd = ["aws", "s3", "ls", f"{base}/{section}/{hash_dir}/", "--recursive"]
        if profile:
            cmd += ["--profile", profile]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return -1
        for ln in result.stdout.splitlines():
            if not ln.strip():
                continue
            parts = ln.split(maxsplit=3)  # DATE TIME SIZE KEY; keep spacey keys intact
            key = parts[3] if len(parts) >= 4 else ""
            if section == "stac_items" and key.endswith(".json"):
                continue  # stac_items JSON is excluded from sync
            total += 1
    return total


def run_triage_gaps(gaps_tsv: Path, items_dir: Path, profile: Optional[str]) -> int:
    """Classify each coverage gap: source-empty (legit absent) vs source-has-data (real gap)."""
    with gaps_tsv.open(encoding="utf-8") as f:
        gaps = [(r["collection"], r["item_id"]) for r in csv.DictReader(f, delimiter="\t")]
    print(f"Triaging {len(gaps)} gaps from {gaps_tsv}")

    results = []
    empty = real = noprov = errored = 0
    for collection, item_id in gaps:
        prov = read_item_provenance(items_dir, collection, item_id)
        if not prov:
            verdict, src, count = "NO_PROVENANCE", "", -1
            noprov += 1
        else:
            prefix, hash_dir = prov
            src = f"{prefix}/{hash_dir}"
            count = count_source_objects(prefix, hash_dir, profile)
            if count < 0:
                verdict = "ERROR"
                errored += 1
            elif count == 0:
                verdict = "SOURCE_EMPTY"  # legit absent — nothing to sync
                empty += 1
            else:
                verdict = "REAL_GAP"  # source has assets that never landed
                real += 1
        results.append((collection, item_id, src, verdict, count))

    with TRIAGE_TSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["collection", "item_id", "source", "verdict", "source_object_count"])
        w.writerows(results)

    print(f"\n  SOURCE_EMPTY (legit absent): {empty}")
    print(f"  REAL_GAP (needs sync):       {real}")
    print(f"  NO_PROVENANCE:               {noprov}")
    print(f"  ERROR:                       {errored}")
    print(f"  Wrote {len(results)} rows to {TRIAGE_TSV}")

    real_gaps = [r for r in results if r[3] == "REAL_GAP"]
    if real_gaps:
        print(f"\nACTION NEEDED: {len(real_gaps)} real gap(s) have source data but no destination assets:")
        for r in real_gaps[:20]:
            print(f"  {r[0]}/{r[1]}  (source {r[2]}, {r[4]} objects)")
        return 1
    print("\nNo real gaps — all coverage gaps are source-empty (legitimately absent).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile sync_assets results against destination S3")
    parser.add_argument("--data-root", help="Destination data root, e.g. s3://fimc-data/hv-fim-dev-data")
    parser.add_argument("--progress-log", default=DEFAULT_PROGRESS_LOG, type=Path)
    parser.add_argument("--missing-tsv", default=DEFAULT_MISSING_TSV, type=Path)
    parser.add_argument("--missing-sample", type=int, default=200, help="Rows of missing.tsv to spot-check (default 200)")
    parser.add_argument("--profile", default=None, help="AWS profile for destination reads")
    parser.add_argument("--seed", type=int, default=0, help="Random seed for reproducible sampling")
    parser.add_argument("--full", action="store_true",
                        help="Full-coverage diff: walk post-rewrite items dir vs one bulk S3 list")
    parser.add_argument("--triage-gaps", default=None, type=Path, metavar="GAPS_TSV",
                        help="Classify coverage_gaps.tsv rows: source-empty (legit) vs real-gap (needs sync)")
    parser.add_argument("--working-dir", default=DEFAULT_WORKING_DIR,
                        help="Post-rewrite working dir (items/<collection>/<item>/) for --full / --triage-gaps")
    args = parser.parse_args()

    if args.triage_gaps:
        items_dir = Path(args.working_dir).expanduser() / "items"
        if not args.triage_gaps.exists():
            print(f"ERROR: gaps TSV not found: {args.triage_gaps}", file=sys.stderr)
            return 1
        if not items_dir.exists():
            print(f"ERROR: items dir not found: {items_dir}", file=sys.stderr)
            return 1
        return run_triage_gaps(args.triage_gaps, items_dir, args.profile)

    if not args.data_root:
        print("ERROR: --data-root is required (except with --triage-gaps)", file=sys.stderr)
        return 1

    if args.full:
        items_dir = Path(args.working_dir).expanduser() / "items"
        if not items_dir.exists():
            print(f"ERROR: items dir not found: {items_dir}", file=sys.stderr)
            return 1
        return run_full_coverage(items_dir, args.data_root, args.profile)

    random.seed(args.seed)

    run1 = parse_run1_failures(args.progress_log)
    print(f"Run-1 failures parsed from log: {len(run1)}")

    missing = load_missing(args.missing_tsv)
    missing_sample = stratified_sample(missing, args.missing_sample)
    print(f"missing.tsv total: {len(missing)} | sampling: {len(missing_sample)}")
    print(f"Destination: {args.data_root}/hec-ras/")
    print()

    all_results = []
    print("Checking run-1 failures (the blind spot)...")
    all_results += reconcile("run1_failure", run1, args.data_root, args.profile)
    print("Checking missing.tsv sample (already-synced vs empty-source)...")
    all_results += reconcile("missing_sample", missing_sample, args.data_root, args.profile)

    with OUTPUT_TSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["set", "collection", "item_id", "source", "verdict", "object_count"])
        w.writerows(all_results)
    print(f"\nWrote {len(all_results)} rows to {OUTPUT_TSV}")

    absent_run1 = [r for r in all_results if r[0] == "run1_failure" and r[4] == "ABSENT"]
    if absent_run1:
        print(f"\nACTION NEEDED: {len(absent_run1)} run-1 failure(s) are ABSENT on the destination:")
        for r in absent_run1:
            print(f"  {r[1]}/{r[2]}  (source {r[3]})")
        return 1

    print("\nAll run-1 failures are PRESENT on the destination — blind spot clear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

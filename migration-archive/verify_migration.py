"""
Verify the full migration landed correctly on S3.

Run AFTER migrate.py has completed against the destination buckets.

Usage:
    python verify_migration.py
    python verify_migration.py 2>&1 | tee verify_migration.log

Env vars:
    STAC_BUCKET_PREFIX   e.g. s3://hv-fim-dev-stac/hec-ras-stac  (required)
    DATA_BUCKET_PREFIX   e.g. s3://hv-fim-dev-data/hec-ras        (required)
    WORKING_DIR          local working dir (default: ~/ras-stac-migration)
    SAMPLE_N             items per program for HREF check (default: 50)
    SURVIVOR_SAMPLE_N    survivor rows per bucket to spot-check (default: 10)
    DROP_SAMPLE_N        drop rows to spot-check for leaks (default: 20)
"""

import csv
import json
import os
import random
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
ENV_FILE = REPO_ROOT / ".env"
CLASSIFICATION_TSV = SCRIPT_DIR / "dump-reconciliation" / "outputs" / "drop_classification.tsv"

PROGRAMS = [("ble_", "ble"), ("mip_", "mip"), ("ohio_rfc", "ohio_rfc")]


def load_env(env_file: Path) -> None:
    if not env_file.exists():
        print(f"Note: {env_file} not found - relying on existing AWS env vars")
        return
    print(f"Loading credentials from {env_file}")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def promote_dest_creds() -> None:
    if os.environ.get("SOURCE_AWS_ACCESS_KEY_ID") and os.environ.get("DEST_AWS_ACCESS_KEY_ID"):
        print("Dual-cred mode detected - promoting DEST_AWS_* -> AWS_* for verification")
        os.environ["AWS_ACCESS_KEY_ID"] = os.environ["DEST_AWS_ACCESS_KEY_ID"]
        os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ["DEST_AWS_SECRET_ACCESS_KEY"]
        token = os.environ.get("DEST_AWS_SESSION_TOKEN")
        if token:
            os.environ["AWS_SESSION_TOKEN"] = token
        else:
            os.environ.pop("AWS_SESSION_TOKEN", None)


def hr(title: str) -> None:
    print()
    print("=" * 61)
    print(title)
    print("=" * 61)


def aws(*args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["aws"] + list(args), capture_output=True, text=True)
    if r.stdout:
        print(r.stdout, end="")
    if r.stderr:
        print(r.stderr, end="", file=sys.stderr)
    return r


def s3_get(key: str) -> dict | None:
    r = subprocess.run(["aws", "s3", "cp", key, "-"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def s3_ls_dirs(prefix: str) -> list[str]:
    r = subprocess.run(["aws", "s3", "ls", prefix + "/"], capture_output=True, text=True)
    return [line.split()[-1].rstrip("/") for line in r.stdout.splitlines() if line.strip().endswith("/")]


def s3_exists(key: str) -> bool:
    u = urlparse(key)
    r = subprocess.run(
        ["aws", "s3api", "head-object", "--bucket", u.netloc, "--key", u.path.lstrip("/")],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def collection_to_program(collection: str) -> str | None:
    for prefix, program in PROGRAMS:
        if collection.startswith(prefix):
            return program
    return None


def check_link_chain(stac_prefix: str) -> bool:
    """Validate root -> program -> collection -> item link chain. Returns True if PASS."""
    errors = []
    warnings = []

    root = s3_get(f"{stac_prefix}/catalog.json")
    if not root:
        print("FAIL: cannot fetch root catalog.json")
        return False

    root_children = [l["href"] for l in root.get("links", []) if l.get("rel") == "child"]
    if not root_children:
        errors.append("root catalog.json has no child links")

    programs_on_s3 = s3_ls_dirs(stac_prefix)
    programs_with_catalog = [p for p in programs_on_s3 if s3_get(f"{stac_prefix}/{p}/catalog.json") is not None]

    print(f"  root child links:           {len(root_children)}")
    print(f"  program dirs on S3:         {programs_on_s3}")
    print(f"  programs with catalog.json: {programs_with_catalog}")

    for prog in programs_with_catalog:
        if not any(prog in href for href in root_children):
            errors.append(f"root catalog.json missing child link for program: {prog}")

    collections_linked = []
    for prog in programs_with_catalog:
        prog_cat = s3_get(f"{stac_prefix}/{prog}/catalog.json")
        child_links = [l for l in prog_cat.get("links", []) if l.get("rel") == "child"]
        parent_links = [l for l in prog_cat.get("links", []) if l.get("rel") == "parent"]
        if not parent_links:
            errors.append(f"{prog}/catalog.json missing parent link")
        coll_dirs = [c for c in s3_ls_dirs(f"{stac_prefix}/{prog}")
                     if s3_get(f"{stac_prefix}/{prog}/{c}/collection.json") is not None]
        print(f"  {prog}/: {len(child_links)} child links, {len(coll_dirs)} collection dirs on S3")
        for coll in coll_dirs:
            if not any(coll in l["href"] for l in child_links):
                errors.append(f"{prog}/catalog.json missing child link for collection: {coll}")
            collections_linked.append((prog, coll))

    print(f"  checking {len(collections_linked)} collection.json files...")
    item_count = 0
    for prog, coll in collections_linked:
        coll_json = s3_get(f"{stac_prefix}/{prog}/{coll}/collection.json")
        if not coll_json:
            errors.append(f"{prog}/{coll}/collection.json missing or unreadable")
            continue
        if not any(l.get("rel") == "parent" for l in coll_json.get("links", [])):
            warnings.append(f"{prog}/{coll}/collection.json missing parent link")
        item_count += len(s3_ls_dirs(f"{stac_prefix}/{prog}/{coll}"))

    print(f"  total item dirs found: {item_count}")
    print()
    for e in errors:
        print(f"  ERROR: {e}")
    for w in warnings:
        print(f"  WARN:  {w}")

    if not errors:
        print(f"  PASS - link chain intact ({len(programs_with_catalog)} programs, "
              f"{len(collections_linked)} collections, {item_count} items)")
        return True
    else:
        print(f"  FAIL - {len(errors)} link-chain error(s)")
        return False


def _load_classification(tsv_path: Path) -> list[dict]:
    rows = []
    with tsv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rows.append(row)
    return rows


def _item_s3_key(stac_prefix: str, collection: str, item_id: str) -> str:
    program = collection_to_program(collection)
    return f"{stac_prefix}/{program}/{collection}/{item_id}/{item_id}.json"


def check_bucket_classifications(stac_prefix: str, tsv_path: Path,
                                  survivor_n: int, drop_n: int) -> bool:
    """Spot-check survivors exist and drops are absent on S3. Returns True if PASS."""
    if not tsv_path.exists():
        print(f"  SKIP - {tsv_path} not found")
        return True

    rows = _load_classification(tsv_path)

    # Count by bucket and decision
    bucket_counts: dict[str, int] = defaultdict(int)
    decision_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        bucket_counts[row["bucket"]] += 1
        decision_counts[row["decision"]] += 1

    print("  Bucket counts:")
    for b in ["A", "B1", "B2a", "B2b", "C", "D", "E", "F"]:
        if bucket_counts.get(b):
            print(f"    {b:<4} {bucket_counts[b]:>7}")
    print(f"  Decision counts: {dict(decision_counts)}")
    print()

    errors = []

    # Survivor spot-checks - group by bucket subtype
    survivors = [r for r in rows if r["decision"] in ("MIGRATE", "KEEP")]
    bucket_a_exact = [r for r in survivors if r["bucket"] == "A" and not r["id_rewrite"]]
    bucket_a_rewrite = [r for r in survivors if r["bucket"] == "A" and r["id_rewrite"]]
    bucket_d = [r for r in survivors if r["bucket"] == "D"]
    bucket_b2b = [r for r in survivors if r["bucket"] == "B2b"]

    for label, group, use_rewrite, use_override in [
        ("A (exact)", bucket_a_exact, False, False),
        ("A (id-rewrite)", bucket_a_rewrite, True, False),
        ("D (override)", bucket_d, False, True),
        ("B2b (keep)", bucket_b2b, False, False),
    ]:
        sample = random.sample(group, min(survivor_n, len(group)))
        hits = misses = 0
        for row in sample:
            collection = row["override_collection"] if use_override else row["collection"]
            item_id = row["id_rewrite"] if use_rewrite else row["id"]
            if not collection or not item_id:
                continue
            key = _item_s3_key(stac_prefix, collection, item_id)
            if s3_exists(key):
                hits += 1
            else:
                misses += 1
                errors.append(f"MISSING survivor [{label}]: {key}")
        if sample:
            print(f"  Bucket {label}: checked {len(sample)}, found {hits}, missing {misses}")

    # Drop spot-checks - confirm absent from S3
    drops = [r for r in rows if r["decision"] == "DROP" and r["collection"] and r["id"]]
    drop_sample = random.sample(drops, min(drop_n, len(drops)))
    leak_count = 0
    for row in drop_sample:
        collection = row["collection"]
        item_id = row["id"]
        program = collection_to_program(collection)
        if not program:
            continue
        key = _item_s3_key(stac_prefix, collection, item_id)
        if s3_exists(key):
            leak_count += 1
            errors.append(f"LEAKED drop [{row['bucket']}]: {key}")
    print(f"\n  Drop spot-check: {len(drop_sample)} sampled, {leak_count} leaked")

    if errors:
        for e in errors:
            print(f"  ERROR: {e}")
        print(f"  FAIL - {len(errors)} bucket classification error(s)")
        return False

    print("  PASS - bucket classification spot-checks clean")
    return True


def check_asset_hrefs_sampled(working_dir: Path, n_per_program: int) -> bool:
    """HEAD a sample of asset HREFs from destination-layout items. Returns True if 0 misses."""
    items_dir = working_dir / "items"
    if not items_dir.exists():
        print(f"  SKIP - items dir not found at {items_dir}")
        return True

    # Group destination-layout items by program
    by_program: dict[str, list[Path]] = defaultdict(list)
    for p in items_dir.rglob("*.json"):
        rel = p.relative_to(items_dir)
        if len(rel.parts) != 3:
            continue
        collection = rel.parts[0]
        program = collection_to_program(collection)
        if program:
            by_program[program].append(p)

    hit = miss = checked_items = 0
    miss_examples = []

    for program, paths in sorted(by_program.items()):
        sample = random.sample(paths, min(n_per_program, len(paths)))
        prog_hit = prog_miss = 0
        for p in sample:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  skip unreadable {p}: {e}")
                continue
            checked_items += 1
            for k, a in (d.get("assets") or {}).items():
                href = a.get("href", "")
                if not href.startswith("s3://"):
                    continue
                u = urlparse(href)
                r = subprocess.run(
                    ["aws", "s3api", "head-object", "--bucket", u.netloc, "--key", u.path.lstrip("/")],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    prog_hit += 1
                    hit += 1
                else:
                    prog_miss += 1
                    miss += 1
                    if len(miss_examples) < 10:
                        first_err = r.stderr.strip().splitlines()[0] if r.stderr else ""
                        miss_examples.append((str(p.relative_to(items_dir)), k, href, first_err))
        print(f"  {program}: {len(sample)} items sampled, {prog_hit} HREFs hit, {prog_miss} missed")

    print(f"\n  Total items checked: {checked_items}")
    print(f"  Asset HREFs hit:     {hit}")
    print(f"  Asset HREFs MISSED:  {miss}")
    if miss_examples:
        print("  Misses (up to 10):")
        for rel, k, h, err in miss_examples:
            print(f"    {rel}[{k}] -> {h}  ({err})")

    if miss == 0:
        print("  PASS - all sampled asset HREFs resolve")
        return True
    else:
        print(f"  FAIL - {miss} asset HREF(s) did not resolve")
        return False


def check_asset_logs(working_dir: Path) -> bool:
    """Report sync_assets failed/missing TSV counts. Returns True if 0 failures."""
    failed_tsv = working_dir / "sync_assets_failed.tsv"
    missing_tsv = working_dir / "sync_assets_missing.tsv"

    def _read_tsv(path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))

    failed = _read_tsv(failed_tsv)
    missing = _read_tsv(missing_tsv)

    print(f"  sync_assets_failed.tsv:  {len(failed)} item(s)")
    if failed:
        print("  Failed (first 10):")
        for row in failed[:10]:
            print(f"    {row.get('collection')}/{row.get('item_id')}")

    print(f"  sync_assets_missing.tsv: {len(missing)} item(s) (0 files transferred - expected high on re-runs)")
    if missing:
        print("  Missing (first 10):")
        for row in missing[:10]:
            print(f"    {row.get('collection')}/{row.get('item_id')}")

    if failed:
        print("  FAIL - asset sync failures present")
        return False
    print("  PASS - no asset sync failures")
    return True


def main() -> None:
    load_env(ENV_FILE)
    promote_dest_creds()

    stac_prefix = os.environ.get("STAC_BUCKET_PREFIX")
    data_prefix = os.environ.get("DATA_BUCKET_PREFIX")
    if not stac_prefix or not data_prefix:
        print("ERROR: STAC_BUCKET_PREFIX and DATA_BUCKET_PREFIX must be set")
        sys.exit(1)

    working_dir = Path(os.environ.get("WORKING_DIR", Path.home() / "ras-stac-migration"))
    sample_n = int(os.environ.get("SAMPLE_N", "50"))
    survivor_n = int(os.environ.get("SURVIVOR_SAMPLE_N", "10"))
    drop_n = int(os.environ.get("DROP_SAMPLE_N", "20"))

    results: dict[str, bool] = {}

    hr("0. Sanity: AWS identity")
    aws("sts", "get-caller-identity")

    hr("1. STAC bucket structure + counts")
    aws("s3", "ls", f"{stac_prefix}/")
    r = subprocess.run(["aws", "s3", "ls", f"{stac_prefix}/", "--recursive"], capture_output=True, text=True)
    lines = r.stdout.splitlines()
    total = len(lines)
    catalogs = sum(1 for l in lines if l.endswith("catalog.json"))
    collections = sum(1 for l in lines if l.endswith("collection.json"))
    items = sum(1 for l in lines if not l.endswith(("catalog.json", "collection.json")))
    print(f"  Total objects:     {total}")
    print(f"  catalog.json:      {catalogs}")
    print(f"  collection.json:   {collections}")
    print(f"  item.json:         {items}")

    hr("2. Link-chain validation (root -> program -> collection -> item)")
    results["link_chain"] = check_link_chain(stac_prefix)

    hr("3. Bucket classification spot-checks (survivors present, drops absent)")
    results["bucket_checks"] = check_bucket_classifications(
        stac_prefix, CLASSIFICATION_TSV, survivor_n, drop_n)

    hr(f"4. Asset HREF resolution ({sample_n} items per program sampled)")
    results["asset_hrefs"] = check_asset_hrefs_sampled(working_dir, sample_n)

    hr("5. sync_assets log summary")
    results["asset_logs"] = check_asset_logs(working_dir)

    hr("Summary")
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {status}  {name}")
        if not passed:
            all_pass = False

    if not all_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
Verify the subset migration landed correctly on S3.

Run AFTER migrate.py has completed against the target buckets.

Usage:
    python verify_subset.py
    python verify_subset.py 2>&1 | tee verify_subset.log

Env vars (override defaults):
    STAC_BUCKET_PREFIX  e.g. s3://hv-fim-dev-stac/hec-ras-stac
    DATA_BUCKET_PREFIX  e.g. s3://hv-fim-dev-data/hec-ras
    WORKING_DIR         local working dir (default: ~/ras-stac-migration-subset)

Dual-cred mode: if DEST_AWS_* env vars are set, they are promoted to AWS_* so
all verification commands hit the destination bucket.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
ENV_FILE = REPO_ROOT / ".env"


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
    """In dual-cred mode, promote DEST_AWS_* -> AWS_* for verification."""
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


def check_asset_hrefs(working_dir: Path) -> bool:
    """HEAD every asset HREF in destination-layout items. Returns True if 0 misses."""
    items_dir = working_dir / "items"
    if not items_dir.exists():
        print(f"Items dir not found at {items_dir}")
        return False

    hit = miss = checked_items = 0
    miss_examples = []

    for p in items_dir.rglob("*.json"):
        rel = p.relative_to(items_dir)
        if len(rel.parts) != 3 or rel.parts[0] in ("ebfe", "mip_30", "mip_70"):
            continue
        try:
            d = json.loads(p.read_text())
        except Exception as e:
            print(f"  skip unreadable {rel}: {e}")
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
                hit += 1
            else:
                miss += 1
                if len(miss_examples) < 10:
                    first_err = r.stderr.strip().splitlines()[0] if r.stderr else ""
                    miss_examples.append((str(rel), k, href, first_err))

    print(f"Items checked:      {checked_items}")
    print(f"Asset HREFs hit:    {hit}")
    print(f"Asset HREFs MISSED: {miss}")
    if miss_examples:
        print("Misses (up to 10):")
        for rel, k, h, err in miss_examples:
            print(f"  {rel}[{k}] -> {h}  ({err})")

    return miss == 0


def main() -> None:
    load_env(ENV_FILE)
    promote_dest_creds()

    stac_prefix = os.environ.get("STAC_BUCKET_PREFIX", "s3://fimc-data/hv-fim-dev-stac/hec-ras-stac")
    data_prefix = os.environ.get("DATA_BUCKET_PREFIX", "s3://fimc-data/hv-fim-dev-data/hec-ras")
    working_dir = Path(os.environ.get("WORKING_DIR", Path.home() / "ras-stac-migration-subset"))

    hr("0. Sanity: AWS identity")
    aws("sts", "get-caller-identity")

    hr("1. STAC bucket: top-level under hec-ras-stac/")
    aws("s3", "ls", f"{stac_prefix}/")

    hr("2. STAC bucket: recursive count + first 20 entries")
    print("Total objects:")
    r = subprocess.run(["aws", "s3", "ls", f"{stac_prefix}/", "--recursive"], capture_output=True, text=True)
    print(len(r.stdout.splitlines()))
    print("\nFirst 20 entries:")
    print("\n".join(r.stdout.splitlines()[:20]))

    hr("3. Per-item STAC dirs (bucket-A survivors, including id-rewrite case)")
    print("ble/ble_11110105_Poteau/UNT_077_in_LPR/ (bucket A: exact match):")
    aws("s3", "ls", f"{stac_prefix}/ble/ble_11110105_Poteau/UNT_077_in_LPR/")
    print()
    print("ble/ble_12100201_UpperGuadalupe/SPRING_CREEK_1/ (bucket A: id rewritten from SPRING_CREEK):")
    aws("s3", "ls", f"{stac_prefix}/ble/ble_12100201_UpperGuadalupe/SPRING_CREEK_1/")

    hr("4. Data bucket: top-level")
    aws("s3", "ls", f"{data_prefix}/")

    hr("5. Per-item asset dirs (three samples)")
    print("ble_11110105_Poteau/UNT_077_in_LPR/ (cataloged, has thumbnail):")
    aws("s3", "ls", f"{data_prefix}/ble_11110105_Poteau/UNT_077_in_LPR/", "--recursive")
    print()
    print("mip_no_crs/DEEP_FORK_TRIBUTARY/ (no-crs collection, no thumbnail):")
    aws("s3", "ls", f"{data_prefix}/mip_no_crs/DEEP_FORK_TRIBUTARY/", "--recursive")
    print()
    print("mip_03050110/McKenzie_Creek_Tributary_2/ (bucket A, mip_70 source):")
    aws("s3", "ls", f"{data_prefix}/mip_03050110/McKenzie_Creek_Tributary_2/", "--recursive")

    hr("6. Catalog link-chain validation (root -> program -> collection -> item)")
    link_chain_ok = check_link_chain(stac_prefix)

    hr("7. Asset HREF resolution check (every asset HREF should resolve to a real S3 object)")
    hrefs_ok = check_asset_hrefs(working_dir)

    hr("Done. Expected results:")
    print("""  STAC bucket:
    - 1 root catalog.json + 3 program catalog.json (ble/mip/ohio_rfc)
    - 7 collection.json files; 8 item.json files (mip_no_crs has 2 items)
    - Total: 19 STAC objects; ohio_rfc has 1 item (bucket D)
    - 4 items skipped via drop_list.txt; 8 items survive at destination (subset_items.txt has 10 hashes, 2 hashes have 2 items each)
  Data bucket:
    - Per-item dir contains the model files (+ thumbnail.png + .gpkg where present)
  HREF resolution:
    - All asset HREFs should resolve (0 misses)
    - SPRING_CREEK_1 confirms id-rewrite path; Ohio2018a confirms bucket-D override path""")

    if not link_chain_ok or not hrefs_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

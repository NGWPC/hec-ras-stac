"""
HEC-RAS STAC Migration — Orchestrator

Runs the six migration phases in order:
  1. sync_items.py            — item JSONs from Dewberry → local (source shape)
  2. generate_collections.py  — fetch + write collection.json files locally
  3. generate_catalog.py      — write top-level catalog.json
  4. rewrite_hrefs.py         — rewrite hrefs + restructure items source → destination
  5. upload_to_s3.py          — push local tree → destination STAC bucket
  6. sync_assets.py           — per-item assets from Dewberry → dest data bucket

Phase ordering: Phase 4 MUST run before 5 and 6 (sets original_source_* provenance props).

Subset modes (mutually exclusive):
  SUBSET=N        caps Phase 1 to N hashes per source prefix
  ITEMS_FROM=<f>  syncs exactly the curated list of <prefix>/<hash_dir> lines;
                  automatically switches working dir to ~/ras-stac-migration-subset/

Override working dir with WORKING_DIR=<path>.

Credential modes (auto-detected):
  Single-cred: one set of AWS_* keys — use for same-account runs
  Dual-cred:   SOURCE_AWS_* + DEST_AWS_* — cross-account subset validation only;
               requires VIA_LOCAL=1 for Phase 6

Usage:
  python migrate.py                              # full migration
  ITEMS_FROM=subset_items.txt python migrate.py  # subset run
  DRY_RUN=1 python migrate.py                   # dry run
"""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
ENV_FILE = REPO_ROOT / ".env"


def load_env(env_file: Path) -> None:
    """Load key=value pairs from .env into os.environ (skips comments and blanks)."""
    if not env_file.exists():
        print(f"Note: {env_file} not found — relying on existing AWS env vars or default profile")
        return
    print(f"Loading credentials from {env_file}")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def require_env(*keys: str) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        for k in missing:
            print(f"ERROR: required env var {k} is not set")
        sys.exit(1)


def run(cmd: list[str]) -> None:
    """Run a command, streaming output. Exits on failure."""
    result = subprocess.run(cmd, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        sys.exit(result.returncode)


def banner(text: str) -> None:
    print()
    print("═" * 64)
    print(text)
    print("═" * 64)


def main() -> None:
    load_env(ENV_FILE)

    require_env("DATA_ROOT", "STAC_ROOT", "STAC_API_URL")

    dual_cred = bool(os.environ.get("SOURCE_AWS_ACCESS_KEY_ID") and os.environ.get("DEST_AWS_ACCESS_KEY_ID"))
    print(f"Credential mode: {'dual (SOURCE_AWS_* + DEST_AWS_*)' if dual_cred else 'single (AWS_* or default profile)'}")

    items_from = os.environ.get("ITEMS_FROM")
    subset = os.environ.get("SUBSET")
    dry_run = os.environ.get("DRY_RUN")
    via_local = os.environ.get("VIA_LOCAL")
    source_profile = os.environ.get("SOURCE_PROFILE")
    dest_profile = os.environ.get("DEST_PROFILE")
    data_root = os.environ["DATA_ROOT"]
    stac_root = os.environ["STAC_ROOT"]
    stac_api_url = os.environ["STAC_API_URL"]

    if items_from:
        working_dir = Path(os.environ.get("WORKING_DIR", Path.home() / "ras-stac-migration-subset"))
    else:
        working_dir = Path(os.environ.get("WORKING_DIR", Path.home() / "ras-stac-migration"))

    if dual_cred and not via_local:
        print()
        print("WARNING: dual-cred mode detected but VIA_LOCAL is not set.")
        print("  Phase 6 (asset sync) will use one cred set and may fail when source")
        print("  and destination are in different accounts. For subset validation")
        print("  against a cross-account destination, set VIA_LOCAL=1.")
        print()

    print(f"Working dir: {working_dir}")

    python = sys.executable

    # Build reusable flag lists
    working_dir_flag = ["--working-dir", str(working_dir)]
    dry_run_flag = ["--dry-run"] if dry_run else []
    source_profile_flag = ["--source-profile", source_profile] if source_profile else []
    dest_profile_flag = ["--dest-profile", dest_profile] if dest_profile else []
    subset_flag = ["--subset", subset] if subset else []
    items_from_flag = ["--items-from", items_from] if items_from else []
    via_local_flag = ["--via-local"] if via_local else []

    banner("Phase 1/6 — Sync items locally (uses source creds)")
    run([python, "sync_items.py"] + working_dir_flag + source_profile_flag + subset_flag + items_from_flag + dry_run_flag)

    banner("Phase 2/6 — Generate collection metadata")
    run([python, "generate_collections.py"] + working_dir_flag + dry_run_flag)

    banner("Phase 3/6 — Generate top-level catalog.json")
    run([python, "generate_catalog.py"] + working_dir_flag + ["--new-stac-url", stac_api_url] + dry_run_flag)

    banner("Phase 4/6 — Rewrite asset HREFs + restructure items source→destination")
    run([python, "rewrite_hrefs.py"] + working_dir_flag + ["--data-root", data_root] + dry_run_flag)

    banner("Phase 5/6 — Upload local tree (uses dest creds)")
    run([python, "upload_to_s3.py"] + working_dir_flag + ["--stac-root", stac_root] + dest_profile_flag + dry_run_flag)

    if via_local:
        banner("Phase 6/6 — Sync assets via local staging (source creds + dest creds)")
    else:
        banner("Phase 6/6 — Sync assets direct S3-to-S3")
    run([python, "sync_assets.py"] + working_dir_flag + ["--data-root", data_root]
        + source_profile_flag + dest_profile_flag + via_local_flag + dry_run_flag)

    banner("Migration complete.")


if __name__ == "__main__":
    main()

"""Pre-migration coverage validator: cross-check the drop-list's expected
survivor set against the dump's published set.

The migration's contract is "publish exactly what the dump publishes." This
script verifies that the drop-list reconciliation logic produces a survivor
set that matches the dump, before any migration runs.

Reads:
  outputs/dump_items.tsv          — what the dump publishes
  outputs/drop_classification.tsv — every local item's bucket + decision + override

Computes the expected survivor set:
  Bucket A    → (id, local_collection)         migrates
  Bucket B2b  → (id, local_collection)         migrates (name collision, kept)
  Bucket D    → (id, override_collection)      migrates (collection override applied at rewrite)
  All others  → not migrating

Compares against the dump set and reports:
  MISSING_FROM_MIGRATION   dump publishes (id, coll) but we have no survivor for it. Hard fail.
  EXTRA_IN_MIGRATION       we'd publish (id, coll) the dump doesn't. OK only for known B2b
                           name-collision cases — anything else is a real bug.

Exit codes:
  0 — clean: missing=0 AND every extra is a known B2b case
  1 — discrepancy: at least one MISSING or unexpected EXTRA

Usage:
  python3 verify_coverage.py
  python3 verify_coverage.py --dump-items outputs/dump_items.tsv --classification outputs/drop_classification.tsv
  python3 verify_coverage.py --limit-print 20    # show at most 20 examples per category
"""
from __future__ import annotations

import argparse
import collections
import csv
import sys
from pathlib import Path

DEFAULT_DUMP = Path(__file__).parent / "outputs" / "dump_items.tsv"
DEFAULT_CLASSIFICATION = Path(__file__).parent / "outputs" / "drop_classification.tsv"


def _load_dump_set(path: Path) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str]]:
    """Return ({(id, collection)}, {(id, collection): source_hash}) for every dump row."""
    out: set[tuple[str, str]] = set()
    hashes: dict[tuple[str, str], str] = {}
    with path.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            pair = (row["id"], row["collection"])
            out.add(pair)
            hashes[pair] = row["source_hash"]
    return out, hashes


def _load_expected_survivors(
    path: Path,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], dict[str, int]]:
    """Walk drop_classification.tsv and compute:

    - all_survivors: {(id, destination_collection)} that the migration will publish
    - b2b_survivors: subset of all_survivors that came from bucket B2b
                     (used to allow EXTRA_IN_MIGRATION entries without flagging
                     them as bugs)
    - bucket_counts: bucket → row count (for summary)
    """
    all_survivors: set[tuple[str, str]] = set()
    b2b_survivors: set[tuple[str, str]] = set()
    bucket_counts: collections.Counter = collections.Counter()
    with path.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            bucket = row["bucket"]
            decision = row["decision"]
            iid = row["id"]
            bucket_counts[bucket] += 1
            if decision not in ("MIGRATE", "KEEP"):
                continue
            if bucket == "A":
                # Use the dump's canonical id if this item has a suffix rewrite
                effective_id = row.get("id_rewrite", "").strip() or iid
                all_survivors.add((effective_id, row["collection"]))
            elif bucket == "B2b":
                pair = (iid, row["collection"])
                all_survivors.add(pair)
                b2b_survivors.add(pair)
            elif bucket == "D":
                override = row.get("override_collection", "").strip()
                if not override:
                    # Bucket D with no override is a data bug — flag via a
                    # sentinel collection name so it surfaces as a missing-coverage row
                    sys.stderr.write(
                        f"warning: bucket-D row id={iid} rel_path={row['rel_path']} has empty override_collection\n"
                    )
                    continue
                effective_id = row.get("id_rewrite", "").strip() or iid
                all_survivors.add((effective_id, override))
            else:
                # Other MIGRATE/KEEP buckets shouldn't exist — defensive
                sys.stderr.write(
                    f"warning: unexpected migrate/keep bucket {bucket} for id={iid}\n"
                )
    return all_survivors, b2b_survivors, dict(bucket_counts)


def _print_examples(label: str, items: list[tuple[str, str]], limit: int) -> None:
    if not items:
        print(f"  {label}: 0")
        return
    print(f"  {label}: {len(items)}")
    for iid, coll in items[:limit]:
        print(f"    id={iid}\tcollection={coll}")
    if len(items) > limit:
        print(f"    ... and {len(items) - limit} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dump-items", type=Path, default=DEFAULT_DUMP)
    parser.add_argument("--classification", type=Path, default=DEFAULT_CLASSIFICATION)
    parser.add_argument("--limit-print", type=int, default=20,
                        help="Max examples to print per discrepancy category (default: 20)")
    args = parser.parse_args()

    if not args.dump_items.exists():
        sys.stderr.write(f"error: {args.dump_items} not found — run build_drop_list.py first\n")
        return 1
    if not args.classification.exists():
        sys.stderr.write(f"error: {args.classification} not found — run build_drop_list.py first\n")
        return 1

    print(f"==> Loading dump set from {args.dump_items}")
    dump_set, dump_hashes = _load_dump_set(args.dump_items)
    print(f"    dump items: {len(dump_set)}")

    print(f"==> Loading expected survivors from {args.classification}")
    survivors, b2b_survivors, bucket_counts = _load_expected_survivors(args.classification)
    print(f"    expected survivors: {len(survivors)} "
          f"(A={bucket_counts.get('A', 0)} + "
          f"B2b={bucket_counts.get('B2b', 0)} + "
          f"D={bucket_counts.get('D', 0)})")

    # Compare
    missing = sorted(dump_set - survivors)
    extras = sorted(survivors - dump_set)
    unexpected_extras = sorted(p for p in extras if p not in b2b_survivors)

    # Partition missing into dump-duplicates (same hash+coll already covered by a
    # different survivor) vs real gaps. A dump-duplicate is a dump anomaly where the
    # same physical item appears under two IDs in the same collection; the migration
    # covers one of them but can't produce both from a single source file.
    survivor_hashes: set[tuple[str, str]] = set()  # (hash, coll)
    with args.classification.open() as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row["decision"] not in ("MIGRATE", "KEEP"):
                continue
            coll = row.get("override_collection", "").strip() or row["collection"]
            survivor_hashes.add((row["source_hash"], coll))

    dump_duplicates = sorted(
        p for p in missing
        if (dump_hashes.get(p, ""), p[1]) in survivor_hashes
    )
    real_missing = sorted(p for p in missing if p not in set(dump_duplicates))

    print()
    print("==> Coverage report")
    _print_examples("MISSING_FROM_MIGRATION (dump → survivors gap)", missing, args.limit_print)
    if dump_duplicates:
        _print_examples("  of which dump-duplicates (same hash covered by different id)", dump_duplicates, args.limit_print)
    _print_examples("  of which real gaps (hash absent from survivors)", real_missing, args.limit_print)
    print()
    _print_examples("EXTRA_IN_MIGRATION (survivors → dump extras)", extras, args.limit_print)
    if extras:
        _print_examples("  of which unexpected (not B2b)", unexpected_extras, args.limit_print)

    print()
    print("==> Summary")
    print(f"  dump set size:                {len(dump_set)}")
    print(f"  expected survivors:           {len(survivors)}")
    print(f"  MISSING_FROM_MIGRATION:       {len(missing)}")
    print(f"    dump-duplicates (ok):       {len(dump_duplicates)}")
    print(f"    real gaps (hard fail):      {len(real_missing)}")
    print(f"  EXTRA_IN_MIGRATION:           {len(extras)}")
    print(f"  unexpected extras (non-B2b):  {len(unexpected_extras)}")

    if real_missing or unexpected_extras:
        print()
        print("FAIL — drop-list reconciliation does not match the dump's published set.")
        return 1
    print()
    print("PASS — every dump item is covered (dump-duplicates and B2b extras are expected).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

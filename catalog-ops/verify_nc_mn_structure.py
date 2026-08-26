"""
Verify the structure of a newly delivered STAC tree against the authoritative catalog.

Built for the NC/MN delivery from Dewberry (see catalog-ops/Model_Integration_Guide.md,
Phase 1 — Structural verification). Walks every collection.json and item JSON in the delivery, compares each
against a known-good reference collection/item from the authoritative catalog, and
reports every structural divergence grouped by type.

The point is to decide, per divergence: "trivial, fix in our rewrite step" vs.
"needs a Dewberry follow-up". Divergences already known and accepted for this
delivery are tagged EXPECTED; anything else is tagged UNEXPECTED and is what
warrants a closer look before migrating.

Read-only: never writes to the delivery tree or the reference tree.

Usage:
    python3 verify_nc_mn_structure.py \
      --delivery-dir ~/Desktop/work/dewberry-stac \
      --reference-collection ~/Desktop/work/hec-ras-stac/ble/ble_05119_Pulaski/collection.json \
      --reference-item ~/Desktop/work/hec-ras-stac/ble/ble_05119_Pulaski/gapck/gapck.json

    # Full per-offender listing rather than capped samples
    python3 verify_nc_mn_structure.py ... --show-all

    # Machine-readable, for diffing between delivery revisions
    python3 verify_nc_mn_structure.py ... --json-report report.json
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Divergences already known/accepted for the NC/MN delivery. Everything else is
# surfaced as UNEXPECTED. Keep this list in sync with the "Fixes applied in the
# rewrite" table in Model_Integration_Guide.md.
EXPECTED_FINDINGS: frozenset[str] = frozenset({
    "collection_missing_title",
    "collection_license_not_proprietary",
    "collection_stac_version_1_1_0",
    "item_projection_ext_v1_1_0",
    "item_uses_proj_epsg_not_proj_code",
    "item_asset_missing_s3_key",
    "item_thumbnail_key_capitalized",
    "item_thumbnail_title_capitalized",
    # Dewberry puts the media type in `roles` (["thumbnail", "image/png"]) instead of a
    # separate `type` field. These two findings are the same root cause, fixed together
    # by the same normalization — the authoritative catalog has roles=["thumbnail"] plus
    # type="image/png".
    "item_thumbnail_type_in_roles",
    "item_thumbnail_missing_type",
    # Found during a byte-for-byte live-deployment validation pass (not caught by the
    # original Phase 1 properties-level scan, since this is a per-asset field): every
    # dewberry .f01 (steady-flow) asset carries `profile_names`, a field verified absent
    # from 100% of a 689-item sample across both ble and mip in the authoritative
    # catalog (number_of_profiles is universal on both sides; profile_names is not).
    # Decision: keep it, don't strip it — same precedent as the MN/NC vendor-namespaced
    # properties (harmless extra provenance, nothing downstream requires its absence).
    "item_flow_asset_has_profile_names",
    "item_missing_fim_group",
    "item_href_points_at_staging_prefix",
    # Both of these occur at comparable-or-worse rates in the authoritative catalog
    # itself (verified against ble_*: ~7% of items have no CRS identifier, ~59% have a
    # null ras_version), so they are pre-existing tolerated conditions in this catalog,
    # not defects introduced by this delivery. Tracked, not escalated.
    "item_null_crs_but_wkt2_present",
    "item_null_ras_version",
})

STAGING_HREF_MARKER = "dewberry-stac/hv-fim-dev-data"
PROJECTION_EXT_PREFIX = "https://stac-extensions.github.io/projection/"
EXPECTED_PROJECTION_EXT = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"

# An item is expected to carry, at minimum, the core HEC-RAS model files. Keyed by
# the role each asset should declare; used to flag items missing model data entirely.
REQUIRED_ASSET_ROLES: frozenset[str] = frozenset({"project-file", "geometry-file", "plan-file"})


class Report:
    """Collects findings keyed by finding type."""

    def __init__(self) -> None:
        self._findings: dict[str, list[str]] = defaultdict(list)
        self._seen: dict[str, set[str]] = defaultdict(set)
        self.collections_checked = 0
        self.items_checked = 0
        self.unreadable: list[str] = []

    def add(self, finding: str, subject: str) -> None:
        """Record a finding. Repeats of the same (finding, subject) collapse, so counts
        read as 'subjects affected' rather than 'occurrences' — an item with 11 assets
        all missing s3_key counts once, not 11 times."""
        if subject in self._seen[finding]:
            return
        self._seen[finding].add(subject)
        self._findings[finding].append(subject)

    @property
    def findings(self) -> dict[str, list[str]]:
        return dict(self._findings)

    def unexpected_types(self) -> list[str]:
        return sorted(f for f in self._findings if f not in EXPECTED_FINDINGS)


def _load_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        with path.open() as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Unreadable JSON: {path} ({exc})")
        return None


def check_collection(coll: dict[str, Any], ref: dict[str, Any], subject: str, report: Report) -> None:
    """Compare one delivery collection.json against the reference collection."""
    if "title" not in coll:
        report.add("collection_missing_title", subject)
    if coll.get("license") != ref.get("license"):
        report.add("collection_license_not_proprietary", subject)
    if coll.get("stac_version") != ref.get("stac_version"):
        report.add("collection_stac_version_1_1_0", subject)

    ref_keys, coll_keys = set(ref), set(coll)
    for missing in sorted(ref_keys - coll_keys - {"title"}):
        report.add(f"collection_missing_key:{missing}", subject)
    for extra in sorted(coll_keys - ref_keys):
        report.add(f"collection_extra_key:{extra}", subject)

    ref_summaries = set(ref.get("summaries", {}))
    coll_summaries = set(coll.get("summaries", {}))
    for missing in sorted(ref_summaries - coll_summaries):
        report.add(f"collection_missing_summary:{missing}", subject)

    extent = coll.get("extent", {})
    if not extent.get("spatial", {}).get("bbox"):
        report.add("collection_missing_spatial_bbox", subject)
    if not extent.get("temporal", {}).get("interval"):
        report.add("collection_missing_temporal_interval", subject)


def check_item(item: dict[str, Any], ref: dict[str, Any], subject: str, report: Report) -> None:
    """Compare one delivery item JSON against the reference item."""
    if item.get("stac_version") != ref.get("stac_version"):
        report.add(f"item_stac_version:{item.get('stac_version')}", subject)

    exts = item.get("stac_extensions", []) or []
    proj_exts = [e for e in exts if e.startswith(PROJECTION_EXT_PREFIX)]
    for ext in proj_exts:
        if ext != EXPECTED_PROJECTION_EXT:
            report.add("item_projection_ext_v1_1_0" if "v1.1.0" in ext
                       else f"item_projection_ext_unexpected:{ext}", subject)
    if not proj_exts:
        report.add("item_missing_projection_ext", subject)

    props = item.get("properties", {})
    if "proj:epsg" in props and "proj:code" not in props:
        report.add("item_uses_proj_epsg_not_proj_code", subject)
    if "proj:epsg" not in props and "proj:code" not in props:
        report.add("item_missing_crs_identifier", subject)
    # A present-but-null CRS is distinct from an absent one, and distinct again from
    # one that's unrecoverable. The authoritative catalog tolerates null CRS (~7% of
    # BLE items have neither proj:code nor proj:epsg), so null alone isn't a defect —
    # but a null CRS *without* proj:wkt2 to recover it from would be.
    if props.get("proj:epsg") is None and props.get("proj:code") is None:
        if props.get("proj:wkt2"):
            report.add("item_null_crs_but_wkt2_present", subject)
        else:
            report.add("item_null_crs_and_no_wkt2", subject)

    if not props.get("ras_version"):
        report.add("item_null_ras_version", subject)

    ref_props = ref.get("properties", {})
    for key in sorted(set(ref_props) - set(props)):
        # proj:code is covered by the dedicated check above.
        if key != "proj:code":
            report.add(f"item_missing_property:{key}", subject)

    if not item.get("geometry"):
        report.add("item_missing_geometry", subject)
    if not item.get("bbox"):
        report.add("item_missing_bbox", subject)
    if not item.get("collection"):
        report.add("item_missing_collection_field", subject)
    if "fim_group" not in item:
        report.add("item_missing_fim_group", subject)

    _check_assets(item.get("assets", {}), subject, report)


def _check_assets(assets: dict[str, Any], subject: str, report: Report) -> None:
    if not assets:
        report.add("item_no_assets", subject)
        return

    seen_roles: set[str] = set()
    for key, asset in assets.items():
        seen_roles.update(asset.get("roles", []) or [])

        href = asset.get("href", "")
        if not href:
            report.add("item_asset_missing_href", f"{subject}#{key}")
        elif STAGING_HREF_MARKER in href:
            report.add("item_href_points_at_staging_prefix", subject)

        if "s3_key" not in asset:
            report.add("item_asset_missing_s3_key", subject)

        if key.lower() == "thumbnail":
            if key != "thumbnail":
                report.add("item_thumbnail_key_capitalized", subject)
            roles = asset.get("roles", []) or []
            if any("/" in r for r in roles):
                report.add("item_thumbnail_type_in_roles", subject)
            if "type" not in asset:
                report.add("item_thumbnail_missing_type", subject)
            if asset.get("title", "").lower() == "thumbnail" and asset.get("title") != "thumbnail":
                report.add("item_thumbnail_title_capitalized", subject)

        if key.lower().endswith(".f01") and "profile_names" in asset:
            report.add("item_flow_asset_has_profile_names", subject)

    missing_roles = REQUIRED_ASSET_ROLES - seen_roles
    if missing_roles == REQUIRED_ASSET_ROLES:
        report.add("item_missing_all_core_ras_assets", subject)
    elif missing_roles:
        report.add(f"item_missing_asset_roles:{','.join(sorted(missing_roles))}", subject)


def walk_delivery(delivery_dir: Path, ref_coll: dict[str, Any], ref_item: dict[str, Any],
                  report: Report) -> None:
    """Walk every collection.json and item JSON under the delivery tree."""
    for coll_path in sorted(delivery_dir.glob("*/*/collection.json")):
        cid = coll_path.parent.name
        coll = _load_json(coll_path)
        if coll is None:
            report.unreadable.append(str(coll_path))
            continue
        report.collections_checked += 1
        check_collection(coll, ref_coll, cid, report)

        for item_path in sorted(coll_path.parent.glob("*/*.json")):
            item = _load_json(item_path)
            if item is None:
                report.unreadable.append(str(item_path))
                continue
            report.items_checked += 1
            check_item(item, ref_item, f"{cid}/{item_path.stem}", report)


def print_report(report: Report, show_all: bool, sample_n: int = 5) -> None:
    print()
    print("=" * 78)
    print("NC/MN DELIVERY STRUCTURE VERIFICATION")
    print("=" * 78)
    print(f"Collections checked: {report.collections_checked}")
    print(f"Items checked:       {report.items_checked}")
    if report.unreadable:
        print(f"Unreadable files:    {len(report.unreadable)}")

    findings = report.findings
    if not findings:
        print("\nNo divergences found — delivery matches the reference structure exactly.")
        return

    for label, predicate in (
        ("EXPECTED (fix in our rewrite step — no vendor action needed)",
         lambda f: f in EXPECTED_FINDINGS),
        ("UNEXPECTED (review these — candidates for Dewberry follow-up)",
         lambda f: f not in EXPECTED_FINDINGS),
    ):
        selected = {f: s for f, s in findings.items() if predicate(f)}
        print(f"\n{'-' * 78}\n{label}\n{'-' * 78}")
        if not selected:
            print("  (none)")
            continue
        for finding in sorted(selected, key=lambda f: (-len(selected[f]), f)):
            subjects = selected[finding]
            print(f"\n  {finding}  [{len(subjects)} affected]")
            shown = subjects if show_all else subjects[:sample_n]
            for s in shown:
                print(f"      {s}")
            if not show_all and len(subjects) > sample_n:
                print(f"      ... and {len(subjects) - sample_n} more (use --show-all)")

    unexpected = report.unexpected_types()
    print(f"\n{'=' * 78}")
    print(f"SUMMARY: {len(findings)} divergence type(s) — "
          f"{len(findings) - len(unexpected)} expected, {len(unexpected)} unexpected")
    if unexpected:
        print("\nUnexpected divergence types needing a decision:")
        for f in unexpected:
            print(f"  - {f} ({len(findings[f])} affected)")
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a delivered STAC tree against the authoritative catalog structure")
    parser.add_argument("--delivery-dir", default="~/Desktop/work/dewberry-stac",
                        help="Root of the delivered STAC tree (contains <state>/<collection>/...)")
    parser.add_argument("--reference-collection",
                        default="~/Desktop/work/hec-ras-stac/ble/ble_05119_Pulaski/collection.json",
                        help="Known-good collection.json from the authoritative catalog")
    parser.add_argument("--reference-item",
                        default="~/Desktop/work/hec-ras-stac/ble/ble_05119_Pulaski/gapck/gapck.json",
                        help="Known-good item JSON from the authoritative catalog")
    parser.add_argument("--show-all", action="store_true",
                        help="List every affected subject instead of a capped sample")
    parser.add_argument("--json-report", type=Path, default=None,
                        help="Also write the findings to this path as JSON")
    args = parser.parse_args()

    delivery_dir = Path(args.delivery_dir).expanduser()
    ref_coll_path = Path(args.reference_collection).expanduser()
    ref_item_path = Path(args.reference_item).expanduser()

    for path, label in ((delivery_dir, "--delivery-dir"),
                        (ref_coll_path, "--reference-collection"),
                        (ref_item_path, "--reference-item")):
        if not path.exists():
            logger.error(f"{label} not found: {path}")
            return 1

    ref_coll = _load_json(ref_coll_path)
    ref_item = _load_json(ref_item_path)
    if ref_coll is None or ref_item is None:
        logger.error("Reference collection/item could not be parsed")
        return 1

    logger.info(f"Reference collection: {ref_coll_path}")
    logger.info(f"Reference item:       {ref_item_path}")
    logger.info(f"Walking delivery:     {delivery_dir}")

    report = Report()
    walk_delivery(delivery_dir, ref_coll, ref_item, report)

    if report.collections_checked == 0:
        logger.error(f"No collection.json found under {delivery_dir}/*/*/ — check --delivery-dir")
        return 1

    print_report(report, args.show_all)

    if args.json_report:
        payload = {
            "collections_checked": report.collections_checked,
            "items_checked": report.items_checked,
            "unreadable": report.unreadable,
            "expected": {f: s for f, s in report.findings.items() if f in EXPECTED_FINDINGS},
            "unexpected": {f: s for f, s in report.findings.items() if f not in EXPECTED_FINDINGS},
        }
        args.json_report.write_text(json.dumps(payload, indent=2))
        logger.info(f"JSON report written to {args.json_report}")

    # Exit 2 signals "unexpected divergences present" — a decision is needed before
    # migrating. Exit 0 means only known/accepted divergences were found.
    return 2 if report.unexpected_types() else 0


if __name__ == "__main__":
    sys.exit(main())

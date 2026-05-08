# HEC-RAS STAC: Deployment Runbook

> **Adapted from BenchmarkCat.** Phases 1 and 5 are functional today and cover the Terraform-driven EC2 stand-up. Phases 2, 3, and 4 are carried over verbatim from BenchmarkCat as **TODO** stubs — the HEC-RAS migration uses different source paths, different scripts (`migrate_ras_stac.py` + `generate_catalog_metadata.py`), and a different prefix layout (`s3://fimc-data/dewberry-stac/` → `hv-fim-dev-stac/hec-ras-stac/` + `hv-fim-dev-data/hec-ras/`). They are kept here as a structural template to flesh out in as a separate task in the near future.

## Overview & Architecture

HEC-RAS STAC is a STAC geospatial catalog being migrated from Dewberry's S3 (`s3://fimc-data/dewberry-stac/`; item HREFs use the legacy alias `s3://fim/`) into the team's internal AWS account, mirroring the BenchmarkCat deployment pattern (pgSTAC + stac-fastapi + STAC Browser + asset-proxy on a single EC2).

| Component | Details |
|-----------|---------|
| EC2 Instance | t3.xlarge (4 vCPU, 16 GB RAM) |
| Services | PostgreSQL (5432), STAC API (8082), STAC Browser (8080), asset-proxy (8083) |
| Storage | `hv-fim-dev-stac` (STAC catalog) and `hv-fim-dev-data` (assets) |
| Bootstrap | Automated via `deployment/terraform/templates/user_data_standalone.sh.tpl` or manually via `deployment/terraform/user-data/owp-bootstrap.sh` |

---

## Phase 0: Prerequisites & Cross-Team Coordination

### 0.1 Gather Environment Details
- AWS Account ID, preferred region (`us-east-1`)
- VPC name, private subnet name pattern
- Route53 hosted zone ID
- SSH key pair name
- Session Manager logging policy ARN

### 0.2 Cross-Account IAM Setup

**Dest side** — create a temporary migration role **if necessary**:
- Role name: `hec-ras-stac-migration-role` (EC2 trust policy)
- Policy 1 (source read): `s3:GetObject` and `s3:ListBucket` on `s3://fimc-data/dewberry-stac/*`
- Policy 2 (dest write): `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` on `s3://hv-fim-dev-stac/*` and `s3://hv-fim-dev-data/*`

**Source side (Dewberry/NGWPC)** — update `fimc-data` bucket policy to grant cross-account read access **if necessary**.

### 0.3 Verify Cross-Account Access #TODO - this may change as the collection metadata and assets will likely be split before handoff
```bash
aws sts get-caller-identity
aws s3 ls s3://fimc-data/dewberry-stac/ | head -5
aws s3 ls s3://fimc-data/dewberry-stac/ebfe/stac_items/ | head -5
```

### 0.4 Create Destination Buckets **if necessary**
```bash
aws s3 mb s3://hv-fim-dev-stac --region us-east-1
aws s3 mb s3://hv-fim-dev-data --region us-east-1
```

**Gate:** Do not proceed until cross-account S3 read is confirmed.

---

## Phase 1: Terraform Infrastructure

### 1.1 Create Configuration

Working dir: `deployment/terraform/`

Create `terraform.tfvars` (template in `deployment/terraform/TF_README.md`):
```hcl
environment        = "test"
aws_region         = "us-east-1"
api_name           = "hec-ras-stac"
hosted_zone_id     = "<ZONE_ID>"
session_manager_logging_policy_arn = "<SSM_POLICY_ARN>"
vpc_name             = "<VPC_NAME>"
subnet_name_pattern  = "<SUBNET_PATTERN>*"
instance_type        = "t3.xlarge"
root_volume_size     = 100
enterprise_mode      = false
s3_read_paths        = ["hv-fim-dev-stac", "hv-fim-dev-data"]
s3_write_paths       = ["hv-fim-dev-stac/hec-ras-stac/*", "hv-fim-dev-data/hec-ras/*"]
backup_s3_uri        = "s3://hv-fim-dev-data/hec-ras/backups/stac-db/"
stac_catalog_path    = "hec-ras-stac/"
log_retention_days   = 7
# key_name = "your-aws-key-pair-name"  # Optional: required for SSH access
```

Create `backend.tf` for remote state (S3 backend recommended).

### 1.2 Deploy
```bash
cd deployment/terraform
terraform init
terraform plan -var-file="terraform.tfvars"
terraform apply -var-file="terraform.tfvars"
```

Creates: Security group (8080/8082/8083 + SSH to VPC), IAM role with dynamic S3 policies, EC2 instance with bootstrap, Route53 A record, CloudWatch log group.

### 1.3 Verify Bootstrap

```bash
terraform output standalone_instance_ip

# Connect via SSH — requires key_name set in terraform.tfvars
# terraform output ssh_instructions prints the command with the correct IP
terraform output ssh_instructions
ssh -i /path/to/your-key.pem ubuntu@<standalone_instance_ip>

cat /var/log/hec-ras-stac/bootstrap.log
/opt/hec-ras-stac/deployment/health-check.sh
docker ps  # Expect: hec-ras-stac-db, hec-ras-stac-api, hec-ras-stac-browser, hec-ras-stac-asset-proxy
```

**Note:** The bootstrap generates utility scripts (`health-check.sh`, `backup-db.sh`, `restart-services.sh`) on the EC2 instance at `/opt/hec-ras-stac/deployment/`. These are not present in the repository.

**Rollback:** `terraform destroy -var-file="terraform.tfvars"`

**State after Phase 1:** 4 containers running, empty database, API on 8082, Browser on 8080, proxy on 8083.

### 1.4 Clone Repository

```bash
sudo git clone <your-hec-ras-stac-repo-url> /opt/hec-ras-stac/repo
```

Verify:
```bash
ls /opt/hec-ras-stac/repo/deployment/
# Expected: migrate_ras_stac.py, generate_catalog_metadata.py, load_catalog.py,
#           rewrite_asset_urls.py, test_asset_proxy.sh, reset_database.sh, etc.
```

---

## Phase 2: S3 Migration

> **TODO — revisit and flesh out when migration scope kicks off.**
>
> The structure below is carried over from BenchmarkCat as a template. HEC-RAS specifics that need to replace it:
> - Source: `s3://fimc-data/dewberry-stac/{ebfe,mip_30,mip_70}/` (not `benchmark/...`)
> - Source HREF alias baked into item JSONs: `s3://fim/` (Dewberry legacy alias) — not the actual bucket
> - Script: `deployment/migrate_ras_stac.py` (+ `generate_catalog_metadata.py` for the catalog/collection JSONs), not `migrate_s3.py`
> - Destination: `s3://hv-fim-dev-stac/hec-ras-stac/` (metadata) and `s3://hv-fim-dev-data/hec-ras/<prefix>/<item-id>/...` (assets)
> - No `PATH_MAPPINGS` table — rewriting is per-item, driven by source `<prefix>/stac_items/<hash>/` paths and the item's own `id` field
> - `--subset N` flag exists for test runs (3 items per prefix is the canonical test slice)
> - Local-source mode is desired so the laptop can drive the rewrite from a `<local-checkout-dir>/{ebfe,mip_30,mip_70}/` layout before pushing to dest S3
>
> See `deployment/ras_stac_migration_plan.md` for the authoritative HEC-RAS migration plan. Update this Phase 2 section to point at it once the migration task is fully scoped.

Script (placeholder — currently BenchmarkCat's): `deployment/s3_migration/migrate_s3.py`
Reference: `deployment/s3_migration/S3_README.md`

### 2.1 Dry Run
```bash
cd /opt/hec-ras-stac/repo/deployment/s3_migration

python3 migrate_s3.py \
  --source-bucket fimc-data \
  --stac-bucket hv-fim-dev-stac \
  --stac-prefix hec-ras-stac \
  --data-bucket hv-fim-dev-data \
  --data-prefix hec-ras \
  --dry-run --verbose
```

Verify all 8 path mappings are displayed (`PATH_MAPPINGS` in `migrate_s3.py`):

| Collection | Source | Destination (under `hv-fim-dev-data/benchmark/`) |
|---|---|---|
| ble-collection | `benchmark/high_resolution_validation_data_ble` | `ble-collection/` |
| ripple-fim-collection | `benchmark/ripple_fim_100` | `ripple-fim-collection/` |
| hwm-collection | `benchmark/high_water_marks/usgs` | `hwm-collection/` |
| nws-fim-collection | `hand_fim/test_cases/nws_test_cases/validation_data_nws` | `nws-fim-collection/` |
| usgs-fim-collection | `hand_fim/test_cases/usgs_test_cases/validation_data_usgs` | `usgs-fim-collection/` |
| gfm-collection | `benchmark/rs/gfm` | `gfm-collection/` |
| iceye-collection | `benchmark/rs/iceye` | `iceye-collection/` |
| gfm-expanded-collection | `benchmark/rs/PI4` | `gfm-expanded-collection/` |
| STAC catalog | `benchmark/stac-bench-cat` | `hv-fim-dev-stac/benchmark-stac/` |

### 2.2 Execute Migration

```bash
# Download catalog + update HREFs + generate copy script
python3 migrate_s3.py \
  --source-bucket fimc-data \
  --stac-bucket hv-fim-dev-stac --stac-prefix hec-ras-stac \
  --data-bucket hv-fim-dev-data --data-prefix hec-ras \
  --generate-copy-commands --skip-upload

# Review sample HREF
cat ~/benchmark-catalog/dest_catalog/gfm-collection/items/*/item.json | jq '.assets[].href' | head -5
# Expected: s3://hv-fim-dev-data/hec-ras/<collection-id>/...

# Review migration manifest
jq 'length' ~/benchmark-catalog/migration_manifest.json

# Copy assets (~8-12 hours)
~/benchmark-catalog/copy_assets.sh

# Monitor in another terminal:
watch -n 30 'aws s3 ls s3://hv-fim-dev-data/hec-ras/ --recursive | wc -l'

# Upload updated catalog
python3 migrate_s3.py \
  --source-bucket fimc-data \
  --stac-bucket hv-fim-dev-stac --stac-prefix hec-ras-stac \
  --data-bucket hv-fim-dev-data --data-prefix hec-ras \
  --skip-download --skip-update
```

**Recovery:** `copy_assets.sh` is idempotent — re-run on failure, it skips existing files.

### 2.3 Verify Migration
```bash
aws s3 ls s3://hv-fim-dev-stac/hec-ras-stac/ --recursive | wc -l
aws s3 ls s3://hv-fim-dev-data/hec-ras/ --recursive | wc -l
aws s3 cp s3://hv-fim-dev-stac/hec-ras-stac/catalog.json - | jq '.'
aws s3 ls s3://hv-fim-dev-data/hec-ras/
```

### 2.4 Enable S3 Versioning

```bash
aws s3api put-bucket-versioning \
  --bucket hv-fim-dev-stac \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-versioning \
  --bucket hv-fim-dev-data \
  --versioning-configuration Status=Enabled
```

### 2.5 Enable Intelligent-Tiering on data bucket

Applies only to `hv-fim-dev-data` — the STAC catalog in `hv-fim-dev-stac` stays in STANDARD to avoid retrieval latency.

```bash
aws s3api put-bucket-intelligent-tiering-configuration \
  --bucket hv-fim-dev-data \
  --id data-tiering \
  --intelligent-tiering-configuration '{
    "Id": "data-tiering",
    "Status": "Enabled",
    "Filter": {"Prefix": "hec-ras/"},
    # If desired, add/modify Tierings.
    # "Tierings": [
    #   {"Days": 90,  "AccessTier": "ARCHIVE_ACCESS"},
    #   {"Days": 180, "AccessTier": "DEEP_ARCHIVE_ACCESS"}
    # ]
  }'
```

---

## Phase 3: Catalog Loading

> **TODO — revisit and flesh out when migration scope kicks off.**
>
> Carried over from BenchmarkCat. For HEC-RAS, the catalog source path will be `s3://hv-fim-dev-stac/hec-ras-stac/` and the expected item count and collection breakdown differ. The `load_catalog.py` script may need light updates for HEC-RAS collection IDs (HUC8-based plus `mip_no_crs` from mip_70). Verify with the actual rewritten catalog before running.

Script: `deployment/scripts/load_catalog.py`

### 3.1 Sync Catalog Locally
```bash
aws s3 sync s3://hv-fim-dev-stac/hec-ras-stac/ ~/stac-catalog/ --exclude "*" --include "*.json"
```

### 3.2 Load to pgstac
```bash
export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)

python3 /opt/hec-ras-stac/repo/deployment/scripts/load_catalog.py \
  ~/stac-catalog --db-host localhost --db-password $PGPASSWORD --dry-run

python3 /opt/hec-ras-stac/repo/deployment/scripts/load_catalog.py \
  ~/stac-catalog --db-host localhost --db-password $PGPASSWORD
```

### 3.3 Verify
```bash
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT collection, COUNT(*) FROM pgstac.items GROUP BY collection ORDER BY collection;"

docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.items;"  # Total ~23,000

curl http://localhost:8082/collections | jq '.collections | length'
```

**Rollback:** Reset database with `deployment/scripts/reset_database.sh --force` and re-load.

---

## Phase 4: Asset URL Rewriting

> **TODO — revisit and flesh out when migration scope kicks off.**
>
> Carried over from BenchmarkCat. For HEC-RAS, the `s3://` HREF prefix being transformed is `s3://hv-fim-dev-data/hec-ras/...` (not `s3://hv-fim-dev-data/benchmark/...`). The `rewrite_asset_urls.py` script already exists in `deployment/` and accepts `--collection-prefix` for scoped runs. Note: `ripple1d-pipeline` reads `s3://` HREFs directly — only run this rewrite *after* confirming the pipeline still resolves assets, or run it with the collection-prefix flag scoped to browser-only collections.

Script: `deployment/scripts/rewrite_asset_urls.py`

### 4.1 Verify Proxy

The asset-proxy service (`deployment/asset-proxy/app.py`) streams S3 content using IAM role credentials with Range request support for COG rendering.

```bash
sudo /opt/hec-ras-stac/repo/deployment/scripts/test_asset_proxy.sh
```

Tests: proxy health endpoint, AWS credentials, sample asset query from DB, direct S3 access, proxy URL serving.

### 4.2 Rewrite
```bash
export HOST_IP=$(hostname -I | awk '{print $1}')
export PGPASSWORD=$(cat /opt/hec-ras-stac/.db_password)

python3 /opt/hec-ras-stac/repo/deployment/scripts/rewrite_asset_urls.py \
  --proxy-url http://${HOST_IP}:8083 \
  --db-host localhost --db-password $PGPASSWORD --dry-run

python3 /opt/hec-ras-stac/repo/deployment/scripts/rewrite_asset_urls.py \
  --proxy-url http://${HOST_IP}:8083 \
  --db-host localhost --db-password $PGPASSWORD
```

Transforms: `s3://hv-fim-dev-data/hec-ras/...` → `http://<HOST_IP>:8083/s3/hv-fim-dev-data/hec-ras/...`

**Note:** Use private VPC IP for internal access, or domain name if DNS is configured for external users.

### 4.3 Verify
```bash
curl -s "http://${HOST_IP}:8082/collections/<collection-id>/items?limit=1" | \
  jq '.features[0].assets[].href'
# All should show http://<HOST_IP>:8083/s3/hv-fim-dev-data/hec-ras/...
```

---

## Phase 5: Post-Deployment

### 5.1 Update .env (if needed)

The `.env` file is generated during bootstrap at `/opt/hec-ras-stac/deployment/.env`. If `S3_BUCKET` or `S3_CATALOG_PATH` are incorrect:
```bash
sed -i 's/^S3_BUCKET=.*/S3_BUCKET=hv-fim-dev-stac/' /opt/hec-ras-stac/deployment/.env
sed -i 's/^S3_CATALOG_PATH=.*/S3_CATALOG_PATH=hec-ras-stac\//' /opt/hec-ras-stac/deployment/.env
sudo /opt/hec-ras-stac/deployment/restart-services.sh
```

### 5.2 Initial Backup
```bash
sudo /opt/hec-ras-stac/deployment/backup-db.sh
```

---

## Rollback Plan

If deployment fails:

1. Preserve logs from `/var/log/hec-ras-stac/`
2. Infrastructure team destroys resources: `terraform destroy -var-file="terraform.tfvars"`
3. Clean S3 destination buckets if needed (optional)

---

## Troubleshooting

### STAC Browser — WebGL Map Not Rendering (Chrome)

If the OpenLayers map is blank in Chrome, WebGL may be disabled:

1. **Enable Hardware Acceleration:** Chrome Settings → System → turn on "Use graphics acceleration when available" → restart Chrome
2. **Enable WebGL flags:** go to `chrome://flags/#ignore-gpu-blocklist` → set "Override software rendering list" to Enabled → restart Chrome
3. Verify at `https://get.webgl.org/` — you should see a spinning cube

Alternatively, use Firefox.

---

## Operational Scripts

| Script | Path |
|--------|------|
| Health Check | `/opt/hec-ras-stac/deployment/health-check.sh` |
| Restart Services | `/opt/hec-ras-stac/deployment/restart-services.sh` |
| Stop Services | `/opt/hec-ras-stac/deployment/stop-services.sh` |
| Start Services | `/opt/hec-ras-stac/deployment/start-services.sh` |
| View Logs | `/opt/hec-ras-stac/deployment/view-logs.sh` |
| Backup Database | `/opt/hec-ras-stac/deployment/backup-db.sh` |
| Test Asset Proxy | `deployment/test_asset_proxy.sh` |
| Rewrite Asset URLs | `deployment/rewrite_asset_urls.py` |

---

## Key Files

| File | Role |
|---|---|
| `deployment/terraform/main.tf` | IAM, SG, EC2, ALB resources |
| `deployment/terraform/variables.tf` | All configurable inputs |
| `deployment/terraform/data.tf` | VPC, subnet, AMI lookups |
| `deployment/terraform/outputs.tf` | API URL, instance IP, SSH instructions |
| `deployment/terraform/templates/user_data_standalone.sh.tpl` | Bootstrap template (used by Terraform) |
| `deployment/terraform/user-data/owp-bootstrap.sh` | Bootstrap script (manual execution) |
| `deployment/migrate_ras_stac.py` | HEC-RAS item rewrite + asset copy script generator |
| `deployment/generate_catalog_metadata.py` | Collection + catalog JSON generator (post-rewrite) |
| `deployment/load_catalog.py` | pgstac catalog loader |
| `deployment/rewrite_asset_urls.py` | S3 → proxy URL rewriter |
| `deployment/test_asset_proxy.sh` | Proxy validation |
| `deployment/reset_database.sh` | Database reset utility |
| `deployment/ras_stac_migration_plan.md` | HEC-RAS-specific migration plan (source of truth for Phases 2–4) |

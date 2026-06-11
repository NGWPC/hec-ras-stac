# HEC-RAS STAC: Deployment Runbook

Covers infrastructure prerequisites, Terraform deployment, and bootstrap
verification. Once the EC2 is up and all four containers are healthy, proceed
to `catalog-ops/Catalog_Operations.md` for catalog loading and asset setup.

## Overview & Architecture

HEC-RAS STAC is a geospatial STAC catalog (~158,000 HEC-RAS flood inundation
models) served via pgSTAC + stac-fastapi + STAC Browser + asset-proxy on a
single EC2 instance.

| Component | Details |
|-----------|---------|
| EC2 Instance | t3.xlarge (4 vCPU, 16 GB RAM) |
| Services | PostgreSQL (5432), STAC API (8082), STAC Browser (8080), asset-proxy (8083) |
| Source (NGWPC) | `s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/` + `s3://fimc-data/hv-fim-dev-data/hec-ras/` |
| Serving buckets (OWP) | `s3://hv-fim-dev-stac/hec-ras-stac/` + `s3://hv-fim-dev-data/hec-ras/` |
| Bootstrap | Automated via `deployment/terraform/templates/user_data_standalone.sh.tpl` |

The catalog and assets are copied once from the NGWPC `fimc-data` source into
OWP's own `hv-fim-dev-*` buckets (Phase 1, before Terraform), which the stack
then serves from.

---

## Phase 0: Prerequisites & Cross-Team Coordination

### 0.1 Gather Environment Details
- AWS Account ID, preferred region (`us-east-1`)
- VPC name, private subnet name pattern
- Route53 hosted zone ID
- SSH key pair name
- Session Manager logging policy ARN

### 0.2 S3 Access Setup

The catalog + assets are **staged into the OWP buckets before Terraform runs**
(Phase 1), from an admin/operator machine — not the EC2. This avoids a
chicken-and-egg: `s3_read_paths` (Phase 2) and the booting EC2 expect the OWP
buckets to already exist and be populated. The EC2 instance role therefore only
ever needs the OWP buckets — it never touches `fimc-data`.

Two access paths, both used by the operator running Phase 1:

1. **NGWPC source** (`s3://fimc-data/...`) — read-only. NGWPC issues
   **temporary AWS keys (≈8 hr)** scoped to `fimc-data`.
2. **OWP serving buckets** (`hv-fim-dev-stac`, `hv-fim-dev-data`) — write, using
   the operator's own OWP credentials.

Create the OWP destination buckets:
```bash
aws s3 mb s3://hv-fim-dev-stac --region us-east-1
aws s3 mb s3://hv-fim-dev-data --region us-east-1
```

### 0.3 Verify Source Access

With the temporary `fimc-data` keys exported:
```bash
aws sts get-caller-identity
aws s3 ls s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/ | head -5
# Expected: catalog.json + ble/ mip/ ohio_rfc/ dirs
aws s3 ls s3://fimc-data/hv-fim-dev-data/hec-ras/ | head -5
# Expected: collection dirs (ble_*, mip_*, ohio_rfc)
```

**Gate:** Do not proceed until source read access to `fimc-data` is confirmed.

---

## Phase 1: Stage Catalog & Assets into OWP Buckets

Run from an admin machine **before** Terraform — the OWP buckets must be
populated before the EC2 boots and `s3_read_paths` consume them. The source read
uses the temporary `fimc-data` keys; the destination write uses your OWP
credentials.

> **Credential note:** `aws s3 sync` cross-account requires read on the source
> and write on the dest. If a single credential set can't do both, stage through
> the admin machine's local disk (`aws s3 sync s3://fimc-data/... ./local/` with
> source keys, then `aws s3 sync ./local/ s3://hv-fim-dev-.../` with OWP keys).

### 1.1 Clone the repository (**admin machine**)

The `rewrite_catalog_hrefs.py` script is needed in step 1.2 — clone the repo
locally before proceeding.

```bash
git clone https://github.com/NGWPC/hec-ras-stac.git ~/hec-ras-stac -b catalog-ops
```

### 1.2 Sync the STAC catalog to local disk
```bash
mkdir -p ~/hec-ras-catalog
aws s3 sync s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/ ~/hec-ras-catalog/
```

### 1.3 Rewrite catalog HREFs

The catalog item JSONs reference the NGWPC source bucket
(`s3://fimc-data/hv-fim-dev-data/...`). Rewrite them to the OWP serving bucket
before pushing to S3, so the static catalog is accurate and no post-load DB
patching is needed.

```bash
python3 ~/hec-ras-stac/catalog-ops/rewrite_catalog_hrefs.py \
  ~/hec-ras-catalog --dry-run   # verify counts first

python3 ~/hec-ras-stac/catalog-ops/rewrite_catalog_hrefs.py \
  ~/hec-ras-catalog
```

Dry run expected output:
```
Files would be updated: 158173
HREFs would be rewritten: 1641945
[DRY RUN] No files written
```

Expected after apply: all item `href` values updated from
`s3://fimc-data/hv-fim-dev-data/hec-ras/...` → `s3://hv-fim-dev-data/hec-ras/...`

### 1.4 Push corrected catalog to OWP bucket
```bash
aws s3 sync ~/hec-ras-catalog/ s3://hv-fim-dev-stac/hec-ras-stac/
```

### 1.5 Sync the assets (large)

```bash
aws s3 sync s3://fimc-data/hv-fim-dev-data/hec-ras/ s3://hv-fim-dev-data/hec-ras/ \
  --exclude "backups/*"
```

The `backups/` prefix (`s3://fimc-data/hv-fim-dev-data/hec-ras/backups/`) holds
NGWPC-side DB snapshots that OWP does not need and should not copy.

`sync` is idempotent — if the temporary key window expires before it finishes,
refresh the `fimc-data` keys and re-run the same command; it skips objects
already copied.

> **If you need to parallelize or run across multiple key windows**, split by
> collection-id prefix. The data bucket is flat (`hec-ras/<collection-id>/...`)
> and `sync` can't scope to an arbitrary string prefix, so filter with
> `--exclude "*" --include "<prefix>/*"` (the `/*` matches nested item files):
> ```bash
> aws s3 sync s3://fimc-data/hv-fim-dev-data/hec-ras/ s3://hv-fim-dev-data/hec-ras/ \
>   --exclude "backups/*" \
>   --exclude "*" --include "mip_*/*"      # or ble_*/*, ohio_rfc/*, mip_11*/* ...
> ```
> Note: `--exclude "backups/*"` must come before `--exclude "*"` or the wildcard exclude will override it.

### 1.6 Verify the staging
```bash
# Catalog object count should match source (~159,316)
aws s3 ls s3://hv-fim-dev-stac/hec-ras-stac/ --recursive | wc -l

# Per-prefix asset spot-check (collection-id prefixes; compare src vs dst counts)
for p in ble_ mip_ ohio_rfc; do
  echo -n "$p src: "; aws s3 ls s3://fimc-data/hv-fim-dev-data/hec-ras/$p --recursive | wc -l
  echo -n "$p dst: "; aws s3 ls s3://hv-fim-dev-data/hec-ras/$p --recursive | wc -l
done

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
```

**Gate:** Source and dest counts match per program before running Terraform.

---

## Phase 2: Terraform Infrastructure

### 2.1 Create Configuration

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

# OWP's own serving buckets. Runtime reads the catalog + assets from here.
s3_read_paths        = ["hv-fim-dev-stac", "hv-fim-dev-data"]

# Write access for DB backups. Source reads from fimc-data use separate
# temporary keys (Phase 0.2), not the instance role. The Phase 1 catalog +
# asset copy runs from the admin machine using OWP credentials directly.
s3_write_paths       = ["hv-fim-dev-stac/hec-ras-stac/*", "hv-fim-dev-data/hec-ras/*"]
backup_s3_uri        = "s3://hv-fim-dev-data/hec-ras/backups/stac-db/"

stac_catalog_path    = "hec-ras-stac/"
log_retention_days   = 7
# key_name = "your-aws-key-pair-name"  # Optional: required for SSH access
```

Create `backend.tf` for remote state (S3 backend recommended).

### 2.2 Deploy
```bash
cd deployment/terraform
terraform init
terraform plan -var-file="terraform.tfvars"
terraform apply -var-file="terraform.tfvars"
```

Creates: Security group (8080/8082/8083 + SSH to VPC), IAM role with dynamic S3 policies, EC2 instance with bootstrap, Route53 A record, CloudWatch log group.

### 2.3 Verify Bootstrap

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

**State after Phase 2:** 4 containers running, empty database, API on 8082, Browser on 8080, proxy on 8083.

### 2.4 Clone Repository

```bash
sudo git clone https://github.com/NGWPC/hec-ras-stac.git /opt/hec-ras-stac/repo -b catalog-ops
```

Verify:
```bash
ls /opt/hec-ras-stac/repo/catalog-ops/
# Expected: Catalog_Operations.md, diagnose_assets.sh, fix_thumbnail_assets.py,     
#           load_catalog.py, reset_database.sh, rewrite_asset_urls.py ...  
```

---

## Phase 3: Post-Deployment

### 3.1 Update .env (if needed)

The `.env` file is generated during bootstrap at `/opt/hec-ras-stac/deployment/.env`. If `S3_BUCKET` or `S3_CATALOG_PATH` are incorrect:
```bash
sed -i 's/^S3_BUCKET=.*/S3_BUCKET=hv-fim-dev-stac/' /opt/hec-ras-stac/deployment/.env
sed -i 's|^S3_CATALOG_PATH=.*|S3_CATALOG_PATH=hec-ras-stac/|' /opt/hec-ras-stac/deployment/.env
sudo /opt/hec-ras-stac/deployment/restart-services.sh
```

**Next:** proceed to `catalog-ops/Catalog_Operations.md` to sync the catalog locally, load it into pgSTAC, rewrite asset URLs, and verify the deployment end-to-end.

---

## Rollback Plan

If deployment fails:

1. Preserve logs from `/var/log/hec-ras-stac/`
2. Destroy resources: `terraform destroy -var-file="terraform.tfvars"`

---

## Operational Scripts

Generated by bootstrap at `/opt/hec-ras-stac/deployment/` (not in repo):

| Script | Purpose |
|---|---|
| `health-check.sh` | Check containers, API, DB, S3 access |
| `restart-services.sh` | Restart all Docker containers |
| `stop-services.sh` | Stop all containers |
| `start-services.sh` | Start all containers |
| `view-logs.sh` | Tail all container logs |
| `backup-db.sh` | Dump pgSTAC DB and upload to S3 |

---

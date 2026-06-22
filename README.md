# HEC-RAS STAC

This repository houses the deployment and migration of the HEC-RAS STAC Catalog and its referenced assets — ~158,000 HEC-RAS flood inundation models served via pgSTAC + stac-fastapi + STAC Browser + asset-proxy on a single EC2 instance.

![ETL Pipeline](ras-stac-etl-pipeline.drawio.png)

## Prerequisites

- AWS credentials for both **NGWPC** (`fimc-data` read, temporary ~8hr keys) and **OWP** (`hv-fim-dev-*` write)
- Terraform ≥ 1.5
- AWS CLI v2
- Python 3.10+
- Access to the target VPC, subnet, Route53 hosted zone, and an EC2 SSH key pair

## Stack

| Container | Port | Role |
|-----------|------|------|
| `hec-ras-stac-db` | 5432 | PostgreSQL + pgSTAC |
| `hec-ras-stac-api` | 8082 | stac-fastapi (OGC STAC API) |
| `hec-ras-stac-browser` | 8080 | STAC Browser (UI) |
| `hec-ras-stac-asset-proxy` | 8083 | Asset proxy (S3 → HTTP) |

All four run on a single `t3.xlarge` EC2 instance provisioned by Terraform.

## Repo Structure

```
hec-ras-stac/
├── deployment/          # Terraform + runbook for EC2 provisioning
│   ├── terraform/       # Infrastructure-as-code (main.tf, variables.tf, user-data)
│   └── Deployment_Runbook.md
├── catalog-ops/         # Scripts run on EC2 post-deploy (load, rewrite, verify)
│   └── Catalog_Operations.md
└── migration-archive/   # One-time ETL: generate STAC catalog from source HEC-RAS data (completed)
```

## S3 Layout

| Role | Bucket | Prefix |
|------|--------|--------|
| Source (NGWPC) | `s3://fimc-data` | `hv-fim-dev-stac/hec-ras-stac/` (catalog JSONs) |
| Source (NGWPC) | `s3://fimc-data` | `hv-fim-dev-data/hec-ras/<collection-id>/` (assets) |
| Serving (OWP) | `s3://hv-fim-dev-stac` | `hec-ras-stac/` (catalog JSONs) |
| Serving (OWP) | `s3://hv-fim-dev-data` | `hec-ras/<collection-id>/` (assets) |

Catalog and assets are copied once from `fimc-data` into the OWP buckets before Terraform runs. The EC2 instance role only touches the OWP buckets.

## Workflow

Deployment follows two sequential phases:

1. **Infrastructure → [`deployment/Deployment_Runbook.md`](deployment/Deployment_Runbook.md)**
   Provision OWP S3 buckets, copy the STAC catalog and assets from `fimc-data`, run Terraform to stand up the EC2 instance, and verify all four containers are healthy.

2. **Catalog ops → [`catalog-ops/Catalog_Operations.md`](catalog-ops/Catalog_Operations.md)**
   From the running EC2: sync catalog JSONs locally, load them into pgSTAC, rewrite asset URLs to point at the OWP serving buckets, and verify the asset proxy end-to-end.

Phase 2 cannot start until Phase 1 is complete (healthy stack, repo cloned to `/opt/hec-ras-stac/repo`).

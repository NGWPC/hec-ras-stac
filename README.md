# HEC-RAS STAC

This repository houses the deployment and migration of the HEC-RAS STAC Catalog and its referenced assets.

## Workflow

The deployment follows two sequential phases, each covered by its own doc:

1. **Infrastructure → [`deployment/Deployment_Runbook.md`](deployment/Deployment_Runbook.md)**
   Provision OWP S3 buckets, copy the STAC catalog and assets from the NGWPC source (`fimc-data`), run Terraform to stand up the EC2 instance, and verify all four containers are healthy.

2. **Catalog ops → [`catalog-ops/Catalog_Operations.md`](catalog-ops/Catalog_Operations.md)**
   From the running EC2: sync catalog JSONs locally, load them into pgSTAC, rewrite asset URLs to point at the OWP serving buckets, and verify the asset proxy end-to-end.

Phase 2 cannot start until Phase 1 is complete (healthy stack, repo cloned to `/opt/hec-ras-stac/repo`).

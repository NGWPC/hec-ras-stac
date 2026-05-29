#!/bin/bash
################################################################################
# HEC-RAS STAC - OWP Standalone Bootstrap Script
#
# Purpose: Self-contained setup script
# Instance Type: t3.xlarge (4 vCPU, 16 GB RAM)
# OS: Ubuntu 22.04 or Ubuntu 24.04
#
#
################################################################################

set -euxo pipefail

# Configuration Variables - CUSTOMIZE THESE FOR YOUR ENVIRONMENT
INSTALL_DIR="/opt/hec-ras-stac"
LOG_DIR="/var/log/hec-ras-stac"
BACKUP_DIR="/opt/backups/postgres"

# AWS Configuration
AWS_REGION="us-east-1"
BACKUP_S3_URI="s3://hv-fim-dev-data/hec-ras/backups/stac-db/"
DOMAIN_NAME="localhost"

# Database Configuration
POSTGRES_USER="pgstac"
POSTGRES_DB="stacdb"
POSTGRES_PASSWORD=""  # Will be auto-generated if left empty

# Docker Image Versions
PGSTAC_VERSION="v0.8.6"
STAC_API_VERSION="latest"
STAC_BROWSER_VERSION="latest"

################################################################################
# DO NOT EDIT BELOW THIS LINE UNLESS YOU KNOW WHAT YOU'RE DOING
################################################################################

# Detect OS version
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_VERSION=$VERSION_ID
else
    echo "Cannot detect OS version. Exiting."
    exit 1
fi

echo "================================="
echo "HEC-RAS STAC Bootstrap"
echo "Ubuntu Version: $OS_VERSION"
echo "Timestamp: $(date)"
echo "================================="

################################################################################
# Logging Setup
################################################################################
mkdir -p $LOG_DIR
exec 1> >(tee -a "$LOG_DIR/bootstrap.log")
exec 2>&1
echo "[$(date)] Bootstrap started"

################################################################################
# System Updates and Package Installation
################################################################################
echo "[$(date)] Updating system packages..."

# Ubuntu has a known issues with apt locks when cloud-init is running updates in the background.
wait_for_apt_lock() {
  echo "[$(date)] Checking for APT locks..."
  for i in {1..30}; do
    # Check all three locks. If NONE of them are held, we are good to go.
    if ! sudo fuser /var/lib/dpkg/lock >/dev/null 2>&1 && \
       ! sudo fuser /var/lib/apt/lists/lock >/dev/null 2>&1 && \
       ! sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
      echo "[$(date)] APT locks are clear."
      return 0
    fi
    echo "Waiting for APT locks to release... ($i/30)"
    sleep 5
  done
  echo "[$(date)] ERROR: APT lock timeout after 150 seconds."
  exit 1
}

# Ensure IPv4-only apt (fixes Docker + mirror issues)
echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4

# Wait for system updates to finish (handles cloud-init or other package managers)
while sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 5; done

# Clean apt state completely
wait_for_apt_lock
sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*

# Ubuntu package installation
wait_for_apt_lock
sudo apt-get update -y

wait_for_apt_lock
sudo apt-get upgrade -y

wait_for_apt_lock
sudo apt-get install -y \
    docker.io \
    git \
    htop \
    vim \
    wget \
    curl \
    jq \
    postgresql-client \
    python3 \
    python3-pip \
    unzip

ARCH=$(uname -m)
[ "$ARCH" = "x86_64" ] && URL="x86_64" || URL="aarch64"

curl -s "https://awscli.amazonaws.com/awscli-exe-linux-$URL.zip" -o "awscliv2.zip"
unzip -q awscliv2.zip
sudo ./aws/install --update
aws --version

# Install Python dependencies for catalog loading scripts
echo "[$(date)] Installing Python dependencies..."
PIP_EXTRA_ARGS=""
OS_MAJOR=$(echo "$OS_VERSION" | cut -d. -f1)
if [ "$OS_MAJOR" -ge 23 ] 2>/dev/null; then
    PIP_EXTRA_ARGS="--break-system-packages"
fi
pip3 install --no-cache-dir $PIP_EXTRA_ARGS psycopg2-binary

echo "[$(date)] System packages installed"

################################################################################
# Docker Installation and Configuration
################################################################################
echo "[$(date)] Configuring Docker..."

# Conditional for local Docker - can remove for user-data
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable docker
  systemctl start docker
fi


# Add ubuntu user to docker group
if id "ubuntu" &>/dev/null; then
    sudo usermod -a -G docker ubuntu
    RUNTIME_USER="ubuntu"
else
    RUNTIME_USER="root"
fi

sudo chmod 666 /var/run/docker.sock

docker --version

################################################################################
# Docker Compose Installation
################################################################################
if ! command -v docker-compose &> /dev/null; then
    echo "[$(date)] Installing Docker Compose..."

    sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
        -o /usr/local/bin/docker-compose

    sudo chmod +x /usr/local/bin/docker-compose
    sudo ln -sf /usr/local/bin/docker-compose /usr/bin/docker-compose
fi

docker-compose --version

################################################################################
# Directory Structure
################################################################################
echo "[$(date)] Creating directory structure..."

sudo mkdir -p $INSTALL_DIR/deployment
sudo mkdir -p $INSTALL_DIR/scripts
sudo mkdir -p $LOG_DIR
sudo mkdir -p $BACKUP_DIR
sudo mkdir -p /opt/stac/logs

if [ "$RUNTIME_USER" != "root" ]; then
    # Change ownership of directories (ignore errors for read-only mounts like test environments)
    sudo chown -R $RUNTIME_USER:$RUNTIME_USER $INSTALL_DIR/deployment $LOG_DIR $BACKUP_DIR /opt/stac 2>/dev/null || true
    # Try to chown scripts directory, but ignore if it's read-only (e.g., mounted volume in testing)
    sudo chown -R $RUNTIME_USER:$RUNTIME_USER $INSTALL_DIR/scripts 2>/dev/null || true
fi

################################################################################
# Generate Secure Password
################################################################################
if [ -z "$POSTGRES_PASSWORD" ]; then
    echo "[$(date)] Generating secure database password..."
    POSTGRES_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-25)
    echo "$POSTGRES_PASSWORD" | sudo tee $INSTALL_DIR/.db_password > /dev/null
    sudo chmod 600 $INSTALL_DIR/.db_password
    echo "[$(date)] Database password saved to $INSTALL_DIR/.db_password"
fi

################################################################################
# Create .env File
################################################################################
echo "[$(date)] Creating environment configuration..."

PRIMARY_S3_BUCKET=""

cat > $INSTALL_DIR/deployment/.env <<EOF
################################################################################
# HEC-RAS STAC - OWP Environment Configuration
# Generated: $(date)
################################################################################

# Database Configuration
POSTGRES_USER=$POSTGRES_USER
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_DB=$POSTGRES_DB
POSTGRES_HOST=database
POSTGRES_PORT=5432

# STAC API Configuration

STAC_API_URL="http://$DOMAIN_NAME:8082"
STAC_API_TITLE=OWP HEC-RAS STAC API
STAC_API_DESCRIPTION=HEC-RAS STAC catalog
API_PORT=8082
BROWSER_PORT=8080
S3_BUCKET=$PRIMARY_S3_BUCKET
S3_CATALOG_PATH="hec-ras-stac/"
# Docker Image Versions
PGSTAC_VERSION="v0.8.6"
STAC_API_VERSION="latest"
STAC_BROWSER_VERSION="latest"
SB_maxPreviewsOnMap=-1

# AWS Configuration
AWS_REGION=$AWS_REGION

# GDAL VSI Configuration
VSI_CACHE=TRUE
VSI_CACHE_SIZE=1000000000
GDAL_CACHEMAX=512
GDAL_HTTP_MAX_RETRY=3
GDAL_HTTP_RETRY_DELAY=1
CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.gpkg,.json,.parquet
GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
CPL_VSIL_CURL_USE_HEAD=NO

# Application Performance
WEB_CONCURRENCY=10
DB_MIN_CONN_SIZE=5
DB_MAX_CONN_SIZE=50

# Logging
LOG_LEVEL=INFO
EOF

################################################################################
# Create Asset Proxy Service Files
################################################################################
echo "[$(date)] Creating asset proxy service..."

mkdir -p $INSTALL_DIR/deployment/asset-proxy

cat > $INSTALL_DIR/deployment/asset-proxy/app.py <<'PROXY_APP_EOF'
#!/usr/bin/env python3
"""
S3 Asset Proxy Service for STAC
Streams S3 assets directly using IAM role credentials
"""
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import boto3
from botocore.exceptions import ClientError
import uvicorn

app = FastAPI(title="STAC Asset Proxy")

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['GET', 'HEAD'],
    allow_headers=['*'],
)

# S3 client with IAM role credentials
s3_client = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-1'))


@app.get('/health')
@app.head('/health')
def health_check():
    """Health check endpoint"""
    return {'status': 'ok', 'service': 'asset-proxy'}


@app.head('/s3/{bucket}/{path:path}')
def head_s3_asset(bucket: str, path: str):
    """
    HEAD request for S3 asset metadata.

    Args:
        bucket: S3 bucket name
        path: Object key path

    Returns:
        Response with S3 object metadata headers
    """
    try:
        response = s3_client.head_object(
            Bucket=bucket,
            Key=path
        )

        headers = {
            'Content-Type': response.get('ContentType', 'application/octet-stream'),
            'Content-Length': str(response.get('ContentLength', 0)),
            'Last-Modified': response.get('LastModified', '').strftime('%a, %d %b %Y %H:%M:%S GMT') if response.get('LastModified') else '',
            'ETag': response.get('ETag', ''),
            'Accept-Ranges': 'bytes',
        }

        return Response(headers=headers, status_code=200)
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == '404':
            raise HTTPException(status_code=404, detail=f"Object not found: {bucket}/{path}")
        else:
            raise HTTPException(status_code=403, detail=f"Access denied: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get('/s3/{bucket}/{path:path}')
def proxy_s3_asset(bucket: str, path: str, request: Request):
    """
    Stream S3 asset content directly using IAM role credentials.

    Supports HTTP Range requests for COG/GeoTIFF rendering in browsers.

    Args:
        bucket: S3 bucket name
        path: Object key path
        request: FastAPI request object

    Returns:
        StreamingResponse with S3 object content (full or partial)
    """
    try:
        # First get object metadata to know the total size
        head_response = s3_client.head_object(
            Bucket=bucket,
            Key=path
        )
        total_size = head_response['ContentLength']

        # Parse Range header if present
        range_header = request.headers.get('range')

        if range_header and range_header.startswith('bytes='):
            # Parse range (e.g., "bytes=0-1023")
            range_spec = range_header.replace('bytes=', '')
            range_parts = range_spec.split('-')

            start = int(range_parts[0]) if range_parts[0] else 0
            end = int(range_parts[1]) if len(range_parts) > 1 and range_parts[1] else total_size - 1

            # Ensure end doesn't exceed file size
            end = min(end, total_size - 1)
            content_length = end - start + 1

            # Get object with range
            response = s3_client.get_object(
                Bucket=bucket,
                Key=path,
                Range=f'bytes={start}-{end}'
            )

            # Stream the partial content
            def generate():
                for chunk in response['Body'].iter_chunks(chunk_size=65536):
                    yield chunk

            headers = {
                'Content-Type': head_response.get('ContentType', 'application/octet-stream'),
                'Content-Length': str(content_length),
                'Content-Range': f'bytes {start}-{end}/{total_size}',
                'Accept-Ranges': 'bytes',
                'Last-Modified': head_response.get('LastModified', '').strftime('%a, %d %b %Y %H:%M:%S GMT') if head_response.get('LastModified') else '',
                'ETag': head_response.get('ETag', ''),
                'Cache-Control': 'public, max-age=3600',
            }

            return StreamingResponse(
                generate(),
                status_code=206,  # Partial Content
                media_type=head_response.get('ContentType', 'application/octet-stream'),
                headers=headers
            )

        else:
            # No range request - return full content
            response = s3_client.get_object(
                Bucket=bucket,
                Key=path
            )

            def generate():
                for chunk in response['Body'].iter_chunks(chunk_size=65536):
                    yield chunk

            headers = {
                'Content-Type': response.get('ContentType', 'application/octet-stream'),
                'Content-Length': str(response.get('ContentLength', 0)),
                'Accept-Ranges': 'bytes',
                'Last-Modified': response.get('LastModified', '').strftime('%a, %d %b %Y %H:%M:%S GMT') if response.get('LastModified') else '',
                'ETag': response.get('ETag', ''),
                'Cache-Control': 'public, max-age=3600',
            }

            return StreamingResponse(
                generate(),
                media_type=response.get('ContentType', 'application/octet-stream'),
                headers=headers
            )

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'NoSuchKey':
            raise HTTPException(status_code=404, detail=f"Object not found: {bucket}/{path}")
        elif error_code in ['AccessDenied', '403']:
            raise HTTPException(status_code=403, detail=f"Access denied to {bucket}/{path}")
        else:
            raise HTTPException(status_code=500, detail=f"S3 error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get('PORT', '8083')),
        log_level=os.environ.get('LOG_LEVEL', 'info').lower()
    )
PROXY_APP_EOF

cat > $INSTALL_DIR/deployment/asset-proxy/requirements.txt <<'PROXY_REQ_EOF'
fastapi==0.109.0
uvicorn[standard]==0.27.0
boto3==1.34.0
PROXY_REQ_EOF

cat > $INSTALL_DIR/deployment/asset-proxy/Dockerfile <<'PROXY_DOCKER_EOF'
FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# Run as non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8083

# Run application
CMD ["python", "app.py"]
PROXY_DOCKER_EOF

if [ "$RUNTIME_USER" != "root" ]; then
    sudo chown -R $RUNTIME_USER:$RUNTIME_USER $INSTALL_DIR/deployment/asset-proxy 2>/dev/null || true
fi

echo "[$(date)] Asset proxy service files created"

################################################################################
# Create Docker Compose File
################################################################################
echo "[$(date)] Creating docker-compose configuration..."
echo "[$(date)] BOOTSTRAP_TEST_MODE=${BOOTSTRAP_TEST_MODE:-false}"

cat > $INSTALL_DIR/deployment/docker-compose.yml <<'COMPOSE_EOF'
version: '3.8'

services:
  database:
    container_name: hec-ras-stac-db
    image: ghcr.io/stac-utils/pgstac:${PGSTAC_VERSION}
    environment:
      - POSTGRES_USER=${POSTGRES_USER}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - POSTGRES_DB=${POSTGRES_DB}
      - PGUSER=${POSTGRES_USER}
      - PGPASSWORD=${POSTGRES_PASSWORD}
      - PGDATABASE=${POSTGRES_DB}
    ports:
      - "5432:5432"
    volumes:
      - ./pgdata:/var/lib/postgresql/data
    command:
      - "postgres"
      - "-c"
      - "shared_buffers=2GB"
      - "-c"
      - "effective_cache_size=6GB"
      - "-c"
      - "work_mem=32MB"
      - "-c"
      - "random_page_cost=1.1"
      - "-c"
      - "effective_io_concurrency=200"
      - "-c"
      - "max_connections=100"
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5

  stac-api:
    container_name: hec-ras-stac-api
    image: ghcr.io/stac-utils/stac-fastapi-pgstac:${STAC_API_VERSION}
    environment:
      - POSTGRES_USER=${POSTGRES_USER}
      - POSTGRES_PASS=${POSTGRES_PASSWORD}
      - POSTGRES_DBNAME=${POSTGRES_DB}
      - POSTGRES_HOST_READER=database
      - POSTGRES_HOST_WRITER=database
      - POSTGRES_PORT=5432
      - WEB_CONCURRENCY=${WEB_CONCURRENCY}
      - DB_MIN_CONN_SIZE=${DB_MIN_CONN_SIZE}
      - DB_MAX_CONN_SIZE=${DB_MAX_CONN_SIZE}
      - AWS_REGION=${AWS_REGION}
      - VSI_CACHE=${VSI_CACHE}
      - VSI_CACHE_SIZE=${VSI_CACHE_SIZE}
      - GDAL_CACHEMAX=${GDAL_CACHEMAX}
      - GDAL_HTTP_MAX_RETRY=${GDAL_HTTP_MAX_RETRY}
      - CPL_VSIL_CURL_ALLOWED_EXTENSIONS=${CPL_VSIL_CURL_ALLOWED_EXTENSIONS}
      - GDAL_DISABLE_READDIR_ON_OPEN=${GDAL_DISABLE_READDIR_ON_OPEN}
      - CPL_VSIL_CURL_USE_HEAD=${CPL_VSIL_CURL_USE_HEAD}
      - S3_BUCKET=${S3_BUCKET}
      - S3_CATALOG_PATH=${S3_CATALOG_PATH}
      - STAC_FASTAPI_TITLE=HEC-RAS STAC
      - STAC_FASTAPI_DESCRIPTION=HEC-RAS flood inundation model catalog
    ports:
      - "8082:8082"
    depends_on:
      database:
        condition: service_healthy
    restart: unless-stopped
    command: ["uvicorn", "stac_fastapi.pgstac.app:app", "--host", "0.0.0.0", "--port", "8082"]

  stac-browser:
    container_name: hec-ras-stac-browser
    image: ghcr.io/radiantearth/stac-browser:${STAC_BROWSER_VERSION}
    environment:
      - SB_catalogUrl=${STAC_API_URL}
      - SB_maxPreviewsOnMap=-1
    ports:
      - "8080:8080"
    depends_on:
      - stac-api
    restart: unless-stopped

  asset-proxy:
    container_name: hec-ras-stac-asset-proxy
    build:
      context: ./asset-proxy
      dockerfile: Dockerfile
    network_mode: host
    environment:
      - AWS_REGION=${AWS_REGION}
      - PRESIGNED_URL_EXPIRATION=3600
      - PORT=8083
      - LOG_LEVEL=info
    restart: unless-stopped
COMPOSE_EOF

# Modify docker-compose.yml for test mode (Docker-in-Docker)
if [ "${BOOTSTRAP_TEST_MODE:-false}" = "true" ]; then
    echo "[$(date)] Test mode detected - converting to named volumes for Docker-in-Docker compatibility"
    # Replace bind mount with named volume
    sed -i 's|./pgdata:/var/lib/postgresql/data|pgstac-data:/var/lib/postgresql/data|g' $INSTALL_DIR/deployment/docker-compose.yml

    # Add all services to external network and volumes
    cat >> $INSTALL_DIR/deployment/docker-compose.yml <<'TEST_CONFIG_EOF'

networks:
  default:
    name: hec-ras-stac-test
    external: true

volumes:
  pgstac-data:
    driver: local
TEST_CONFIG_EOF
else
    echo "[$(date)] Production mode - using bind mount at ./pgdata"
fi

################################################################################
# Create Health Check Script
################################################################################
echo "[$(date)] Creating health check script..."

cat > $INSTALL_DIR/deployment/health-check.sh <<HEALTH_EOF
#!/bin/bash

# In test mode (Docker-in-Docker), services are sibling containers reachable
# by container name on the shared network, not via localhost.
if [ "${BOOTSTRAP_TEST_MODE:-false}" = "true" ]; then
    API_HOST="hec-ras-stac-api"
    BROWSER_HOST="hec-ras-stac-browser"
else
    API_HOST="localhost"
    BROWSER_HOST="localhost"
fi

echo "=== HEC-RAS STAC Health Check ==="
echo "Date: \$(date)"
echo ""

# Check Docker containers
echo "--- Docker Containers ---"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""

# Check API endpoint
echo "--- STAC API Health ---"
API_RESPONSE=\$(curl -s http://\${API_HOST}:8082/ 2>/dev/null)
if [ \$? -eq 0 ]; then
    echo "\$API_RESPONSE" | jq -r '.title // "API Running (no title)"' 2>/dev/null || echo "API: Running"
else
    echo "API: NOT RESPONDING"
fi
echo ""

# Check Browser endpoint
echo "--- STAC Browser Health ---"
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://\${BROWSER_HOST}:8080 2>/dev/null || echo "Browser: NOT RESPONDING"
echo ""

# Check database
echo "--- Database Health ---"
docker exec hec-ras-stac-db pg_isready -U pgstac -d stacdb 2>/dev/null || echo "Database: NOT READY"
echo ""

# Check disk usage
echo "--- Disk Usage ---"
df -h / | tail -1
echo ""

# Check memory
echo "--- Memory Usage ---"
free -h | grep Mem
echo ""

# Check CPU load
echo "--- CPU Load ---"
uptime
echo ""

# Check S3 access (if AWS credentials available)
echo "--- S3 Access Test ---"
if aws sts get-caller-identity &>/dev/null; then
    echo "AWS Credentials: OK"
    TEST_BUCKET="hv-fim-dev-data"
    if [ -n "$TEST_BUCKET" ]; then
        aws s3 ls s3://$TEST_BUCKET/ &>/dev/null && echo "S3 Access: OK" || echo "S3 Access: FAILED"
    else
        echo "S3 Access: SKIPPED (No read buckets defined)"
    fi
else
    echo "AWS Credentials: NOT CONFIGURED"
fi
echo ""

echo "=== Health Check Complete ==="
HEALTH_EOF

chmod +x $INSTALL_DIR/deployment/health-check.sh

################################################################################
# Create Backup Script
################################################################################
echo "[$(date)] Creating backup script..."

cat > $INSTALL_DIR/deployment/backup-db.sh <<'BACKUP_EOF'
#!/bin/bash
set -e

BACKUP_DIR=/opt/backups/postgres
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="stacdb_${TIMESTAMP}.sql.gz"
DESTINATION_URI=""

mkdir -p $BACKUP_DIR

echo "[$(date)] Starting database backup..."

# Dump database and compress
docker exec hec-ras-stac-db pg_dump -U pgstac -d stacdb | gzip > "$BACKUP_DIR/$BACKUP_FILE"

if [ -s "$BACKUP_DIR/$BACKUP_FILE" ]; then
    echo "[$(date)] Backup created: $BACKUP_FILE"

    # Upload to S3 if a destination URI is provided
    if [ -n "$DESTINATION_URI" ]; then
        [[ "$DESTINATION_URI" != */ ]] && DESTINATION_URI="${DESTINATION_URI}/"
        aws s3 cp "$BACKUP_DIR/$BACKUP_FILE" "${DESTINATION_URI}$BACKUP_FILE" 2>/dev/null
        echo "[$(date)] Backup uploaded to S3: ${DESTINATION_URI}$BACKUP_FILE"
    else
        echo "[$(date)] S3 Upload skipped: No backup URI configured."
    fi

    # Cleanup old local backups (keep last 7 days)
    find $BACKUP_DIR -name "stacdb_*.sql.gz" -mtime +7 -delete
else
    echo "[$(date)] ERROR: Backup file is empty"
    exit 1
fi
BACKUP_EOF

chmod +x $INSTALL_DIR/deployment/backup-db.sh

################################################################################
# Create Service Management Scripts
################################################################################
echo "[$(date)] Creating service management scripts..."

cat > $INSTALL_DIR/deployment/restart-services.sh <<'RESTART_EOF'
#!/bin/bash
cd /opt/hec-ras-stac/deployment
docker-compose restart
echo "Services restarted. Run health-check.sh to verify."
RESTART_EOF

cat > $INSTALL_DIR/deployment/stop-services.sh <<'STOP_EOF'
#!/bin/bash
cd /opt/hec-ras-stac/deployment
docker-compose down
echo "Services stopped."
STOP_EOF

cat > $INSTALL_DIR/deployment/start-services.sh <<'START_EOF'
#!/bin/bash
cd /opt/hec-ras-stac/deployment
docker-compose up -d
echo "Services started. Run health-check.sh to verify."
START_EOF

cat > $INSTALL_DIR/deployment/view-logs.sh <<'LOGS_EOF'
#!/bin/bash
cd /opt/hec-ras-stac/deployment
docker-compose logs -f --tail=100
LOGS_EOF

chmod +x $INSTALL_DIR/deployment/*.sh

################################################################################
# Pull Docker Images
################################################################################
echo "[$(date)] Pulling Docker images (this may take several minutes)..."

docker pull ghcr.io/stac-utils/pgstac:$PGSTAC_VERSION
docker pull ghcr.io/stac-utils/stac-fastapi-pgstac:$STAC_API_VERSION
docker pull ghcr.io/radiantearth/stac-browser:$STAC_BROWSER_VERSION

echo "[$(date)] Docker images pulled successfully"

################################################################################
# Start Services
################################################################################
echo "[$(date)] Starting HEC-RAS STAC services..."

# Ensure log directories exist and are writable (For Testing (Docker-in-Docker))
sudo mkdir -p /opt/stac/logs
sudo chmod -R 777 /opt/stac/logs

cd $INSTALL_DIR/deployment
docker-compose up -d

echo "[$(date)] Waiting for database initialization (30 seconds)..."
sleep 30

################################################################################
# Verify Database
################################################################################
echo "[$(date)] Verifying database extensions..."

docker exec -i hec-ras-stac-db psql -U $POSTGRES_USER -d $POSTGRES_DB -c "\dx" 2>&1 | grep -q "pgstac" && \
    echo "[$(date)] pgstac extension verified" || \
    echo "[$(date)] Warning: pgstac extension not found (may need manual initialization)"

################################################################################
# Configure Systemd Service
################################################################################
# Only configure systemd if it's available (not in Docker containers)
if command -v systemctl >/dev/null 2>&1; then
    echo "[$(date)] Creating systemd service for auto-start..."

    sudo cat > /etc/systemd/system/hec-ras-stac.service <<'SYSTEMD_EOF'
[Unit]
Description=HEC-RAS STAC Services
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/hec-ras-stac/deployment
ExecStart=/usr/local/bin/docker-compose up -d
ExecStop=/usr/local/bin/docker-compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
SYSTEMD_EOF

    sudo systemctl daemon-reload
    sudo systemctl enable hec-ras-stac.service
    sudo systemctl start hec-ras-stac.service
    echo "[$(date)] Systemd service configured"
else
    echo "[$(date)] Systemd not available (skipping service configuration)"
fi

################################################################################
# Configure Automated Backups
################################################################################
# Only configure cron if it's available (not in Docker containers)
if command -v crontab >/dev/null 2>&1; then
    echo "[$(date)] Configuring automated backups..."

    # Add weekly backup to crontab (Sunday 2 AM)
    if [ "$RUNTIME_USER" != "root" ]; then
        sudo -u $RUNTIME_USER bash -c "(crontab -l 2>/dev/null; echo '0 2 * * 0 $INSTALL_DIR/deployment/backup-db.sh >> $LOG_DIR/backup.log 2>&1') | crontab -"
    else
        (crontab -l 2>/dev/null; echo "0 2 * * 0 $INSTALL_DIR/deployment/backup-db.sh >> $LOG_DIR/backup.log 2>&1") | crontab -
    fi

    echo "[$(date)] Automated weekly backups configured (Sunday 2 AM)"
else
    echo "[$(date)] Cron not available (skipping automated backup configuration)"
fi

################################################################################
# Configure Firewall (if applicable)
################################################################################
if command -v firewall-cmd &> /dev/null; then
    echo "[$(date)] Configuring firewall..."
    sudo firewall-cmd --permanent --add-port=8082/tcp
    sudo firewall-cmd --permanent --add-port=8080/tcp
    sudo firewall-cmd --reload
    echo "[$(date)] Firewall configured"
fi

################################################################################
# Create README
################################################################################
cat > $INSTALL_DIR/README.txt <<'README_EOF'
HEC-RAS STAC Deployment
============================

Installation Directory: /opt/hec-ras-stac
Logs Directory:        /var/log/hec-ras-stac

Services:
- STAC API:     http://<instance-ip>:8082
- STAC Browser: http://<instance-ip>:8080
- Database:     localhost:5432 (internal only)

Database Credentials:
- User:     pgstac
- Password: See /opt/hec-ras-stac/.db_password
- Database: stacdb

Quick Commands:
---------------
Health Check:    /opt/hec-ras-stac/deployment/health-check.sh
View Logs:       /opt/hec-ras-stac/deployment/view-logs.sh
Restart:         /opt/hec-ras-stac/deployment/restart-services.sh
Stop:            /opt/hec-ras-stac/deployment/stop-services.sh
Start:           /opt/hec-ras-stac/deployment/start-services.sh
Manual Backup:   /opt/hec-ras-stac/deployment/backup-db.sh

Test API:
---------
curl http://localhost:8082/
curl http://localhost:8082/collections

Test STAC Browser:
curl http://localhost:8080/

Loading Catalog Data:
---------------------
# 1. Clone the hec-ras-stac repository to get loading scripts
cd /opt/hec-ras-stac
git clone <your-hec-ras-stac-repo-url> repo
# Or copy scripts from your local repository

# 2. Dry run to preview what would be loaded
python3 repo/deployment/scripts/load_catalog.py /path/to/catalog --db-host database --dry-run

# 3. Load STAC catalog
python3 repo/deployment/scripts/load_catalog.py /path/to/catalog --db-host database

Database Access:
----------------
docker exec -it hec-ras-stac-db psql -U pgstac -d stacdb

Common SQL Queries:
  \dt                              # List tables
  \dx                              # List extensions
  SELECT COUNT(*) FROM pgstac.items;      # Count items
  SELECT collection, COUNT(*) FROM pgstac.items GROUP BY collection;

Automated Backups:
------------------
Schedule:  Weekly (Sunday 2 AM)
Location:  ${BACKUP_S3_URI:-"None configured (local backups only)"}
Local:     /opt/backups/postgres/ (last 7 days)

Troubleshooting:
----------------
If services not running:
  docker ps
  /opt/hec-ras-stac/deployment/view-logs.sh
  /opt/hec-ras-stac/deployment/restart-services.sh

If API not responding:
  curl http://localhost:8082/
  docker logs hec-ras-stac-api

If database issues:
  docker logs hec-ras-stac-db
  docker exec hec-ras-stac-db pg_isready -U pgstac -d stacdb
README_EOF

################################################################################
# Final Health Check
################################################################################
echo ""
echo "================================="
echo "Waiting for services to stabilize..."
echo "================================="
sleep 10

$INSTALL_DIR/deployment/health-check.sh

################################################################################
# Print Summary
################################################################################
cat <<SUMMARY

================================================================================
HEC-RAS STAC Deployment - COMPLETE
================================================================================

Installation Directory: $INSTALL_DIR
Logs Directory:         $LOG_DIR
Backup Directory:       $BACKUP_DIR

Deployment Configuration:
  AWS Region:       $AWS_REGION
  S3 Backup URI:    ${BACKUP_S3_URI:-"None configured (Local backups only)"}
  API Version:      $STAC_API_VERSION
  Browser Version:  $STAC_BROWSER_VERSION

Services (verify with health-check.sh):
  STAC API:     http://$DOMAIN_NAME:8082
  STAC Browser: http://$DOMAIN_NAME:8080
  Database:     localhost:5432 (internal)

Database Credentials:
  User:     $POSTGRES_USER
  Password: (stored securely in $INSTALL_DIR/.db_password)
  Database: $POSTGRES_DB

Quick Start:
  Health Check:  $INSTALL_DIR/deployment/health-check.sh
  View Logs:     $INSTALL_DIR/deployment/view-logs.sh
  Test API:      curl http://$DOMAIN_NAME:8082/

Next Steps:
  1. Run health check: $INSTALL_DIR/deployment/health-check.sh
  2. Access STAC Browser: http://$DOMAIN_NAME:8080
  3. Load STAC catalog data (IAM instance profile automatically provisioned for S3 access)
     Example: python3 /opt/hec-ras-stac/deployment/scripts/load_catalog.py /path/to/catalog

Documentation: $INSTALL_DIR/README.txt

Automated Features:
  - Services auto-start on boot via systemd
  - Weekly database backups (Sunday 2 AM)
  - 7-day local backup retention
  - Auto-sync to S3 Backup URI (if configured via Terraform)

================================================================================

SUMMARY

echo "[$(date)] Bootstrap completed successfully!"
echo "[$(date)] Full log available at: $LOG_DIR/bootstrap.log"

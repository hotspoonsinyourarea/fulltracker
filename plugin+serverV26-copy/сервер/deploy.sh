#!/bin/bash
set -euo pipefail

DB_NAME="logs"
DB_USER="logs"
DB_PASS="wrGNCPJfbsm7"
SERVICE_DIR="/opt/vector_service"

echo "=== 1. Installing PostgreSQL + Python ==="
apt update
apt install -y postgresql python3-pip python3-venv

echo "=== 2. Starting PostgreSQL ==="
systemctl enable --now postgresql

echo "=== 3. Creating database user + DB ==="
su - postgres -c "psql -c \"CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';\" 2>/dev/null" || true
su - postgres -c "psql -c \"CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};\" 2>/dev/null" || true

echo "=== 4. Restoring dump ==="
sed -i '/^\\\\restrict /d; /^\\\\unrestrict /d' pg_backup.sql 2>/dev/null || true
PGPASSWORD="${DB_PASS}" psql -h localhost -U "${DB_USER}" -d "${DB_NAME}" < pg_backup.sql

echo "=== 5. Setting up vector_service ==="
mkdir -p "${SERVICE_DIR}"
cp services/vector_service/*.py "${SERVICE_DIR}/"
cp services/vector_service/requirements.txt "${SERVICE_DIR}/"
cp q.sqlite "${SERVICE_DIR}/"

cd "${SERVICE_DIR}"
python3 -m venv venv
source venv/bin/activate
pip install --no-cache-dir -r requirements.txt
python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

echo "=== 6. Creating systemd service ==="
cat > /etc/systemd/system/vector_service.service << 'UNIT'
[Unit]
Description=Vector similarity service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/vector_service
Environment=DB_PATH=/opt/vector_service/q.sqlite
ExecStart=/opt/vector_service/venv/bin/gunicorn --bind 0.0.0.0:5006 --workers 1 --timeout 60 app:app
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now vector_service

echo "=== 7. Проверка ==="
sleep 3
curl -s -X POST http://localhost:5006/similarity \
  -H 'Content-Type: application/json' \
  -d '{"text":"how to fix TypeError in Python"}' | python3 -m json.tool

echo ""
echo "=== Готово ==="

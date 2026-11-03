#!/usr/bin/env bash
set -euo pipefail

echo "=========================================================="
echo "   Kiem tra va cai dat Rclone cho API Server"
echo "=========================================================="

# 1. Kiem tra quyen sudo / root
if [ "$EUID" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

# 2. Kiem tra binary rclone da co tren he thong chua
if command -v rclone &> /dev/null; then
    echo "[OK] Rclone da duoc cai dat san: $(rclone version | head -n 1)"
else
    echo "[INFO] Khong tim thay rclone, dang tien hanh cai dat phien ban moi nhat..."
    
    # Cai dat cac dependency co ban neu thieu
    if command -v apt-get &> /dev/null; then
        $SUDO apt-get update -y
        $SUDO apt-get install -y curl unzip ca-certificates
    elif command -v yum &> /dev/null; then
        $SUDO yum install -y curl unzip ca-certificates
    fi

    # Chay script cai dat chinh thuc tu rclone.org
    curl -fsSL https://rclone.org/install.sh | $SUDO bash
    
    echo "[OK] Cai dat Rclone thanh cong: $(rclone version | head -n 1)"
fi

# 3. Tao thu muc luu tru du lieu mac dinh neu chua ton tai
DEFAULT_DATA_DIR="/var/data/gdrive_sync"
echo "[INFO] Khoi tao thu muc dong bo du lieu tai: ${DEFAULT_DATA_DIR}"
$SUDO mkdir -p "${DEFAULT_DATA_DIR}"
$SUDO chmod -R 775 "${DEFAULT_DATA_DIR}"

# 4. Kiem tra thu muc tam cho Service Account JSON
mkdir -p ./credentials
chmod 700 ./credentials

echo "=========================================================="
echo "   Chuan bi he thong hoan tat!"
echo "   Ban co the chay API server bang: uvicorn main:app --host 0.0.0.0 --port 8000"
echo "=========================================================="

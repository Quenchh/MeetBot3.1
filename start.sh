#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
#  start.sh — MeetBot'u Linux/macOS'ta kurar ve başlatır
#  İlk çalıştırmada .venv oluşturur; requirements.txt her
#  değiştiğinde bağımlılıkları otomatik günceller.
#  Ek argümanlar main.py'ye aktarılır:  ./start.sh --doctor
# ──────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
    echo "[MeetBot] Sanal ortam oluşturuluyor..."
    "$PYTHON_BIN" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
fi

# Kurulu bağımlılıklar requirements.txt ile aynı değilse (ilk kurulum veya güncelleme) yükle
if ! cmp -s requirements.txt .venv/requirements.installed.txt; then
    echo "[MeetBot] Bağımlılıklar yükleniyor / güncelleniyor..."
    .venv/bin/python -m pip install -r requirements.txt
    cp requirements.txt .venv/requirements.installed.txt
fi

if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "[MeetBot] .env dosyası oluşturuldu - ayarları oradan değiştirebilirsiniz."
fi

exec .venv/bin/python main.py "$@"

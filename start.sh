#!/bin/bash

echo "=========================================="
echo "  YouTube Live Streamer - Başlatıcı"
echo "=========================================="
echo ""

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "Hata: Python 3 bulunamadı!"
    echo "Lütfen Python 3'ü yükleyin:"
    echo "  Ubuntu/Debian: sudo apt install python3"
    echo "  Fedora: sudo dnf install python3"
    exit 1
fi

# Check if FFmpeg is installed
if ! command -v ffmpeg &> /dev/null; then
    echo "Hata: FFmpeg bulunamadı!"
    echo "Lütfen FFmpeg'i yükleyin:"
    echo "  Ubuntu/Debian: sudo apt install ffmpeg"
    echo "  Fedora: sudo dnf install ffmpeg"
    echo "  Arch: sudo pacman -S ffmpeg"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Sanal ortam oluşturuluyor..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Sanal ortam aktif ediliyor..."
source venv/bin/activate

# Install requirements
echo "Bağımlılıklar kontrol ediliyor..."
pip install -q -r requirements.txt

# Create upload directory
mkdir -p uploads

echo ""
echo "=========================================="
echo "  YouTube Live Streamer Başlatılıyor..."
echo "=========================================="
echo ""
echo "Web tarayıcınızda açın:"
echo "  http://localhost:5000"
echo ""
echo "Ağdaki diğer cihazlar için:"
echo "  http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "Durdurmak için: Ctrl+C"
echo "=========================================="
echo ""

# Start the application
python3 app.py

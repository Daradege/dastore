#!/bin/bash

set -e

echo "==> Dastore Installer"
echo "==> Checking for Arch Linux..."
if ! [ -f /etc/arch-release ]; then
    echo "This script is intended for Arch Linux based distributions only."
    exit 1
fi
echo "==> Arch Linux detected."

echo "==> Installing dependencies..."
sudo pacman -S --needed --noconfirm python python-gobject gtk4 libadwaita

if ! command -v yay &> /dev/null; then
    echo "==> Yay not found. Installing Yay..."
    sudo pacman -S --needed --noconfirm base-devel git
    git clone https://aur.archlinux.org/yay.git
    cd yay
    makepkg -si --noconfirm --asdeps
    cd ..
    rm -rf yay
fi
echo "==> Dependencies installed."

INSTALL_DIR="/opt/dastore"
echo "==> Installing Dastore to ${INSTALL_DIR}..."
sudo mkdir -p ${INSTALL_DIR}
sudo cp -r src/* ${INSTALL_DIR}/
sudo cp assets/dastore.png ${INSTALL_DIR}/

echo "==> Creating desktop entry..."
sudo cat > /usr/share/applications/dastore.desktop <<EOL
[Desktop Entry]
Type=Application
Name=Dastore
Comment=A graphical package manager for Arch Linux
Exec=${INSTALL_DIR}/main.py
Icon=${INSTALL_DIR}/dastore.png
Categories=System;PackageManager;
Terminal=false
EOL

echo "==> Creating executable link in /usr/local/bin..."
sudo ln -sf ${INSTALL_DIR}/main.py /usr/local/bin/dastore

echo "==> Installation complete! You can now run 'dastore' from your terminal or find it in your app menu."
#!/usr/bin/env bash
# Installe les dépendances système puis tdash (via pipx).
# Usage : ./install.sh [source]      source = dossier local (défaut ".") ou URL "git+https://..."
set -euo pipefail
SRC="${1:-.}"

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
req() { echo "→ $*"; "$@"; }
opt() { for p in "$@"; do $INSTALL "$p" >/dev/null 2>&1 && echo "✔ $p" || echo "⚠ $p indisponible (facultatif, ignoré)"; done; }

if   command -v apt-get >/dev/null; then INSTALL="$SUDO apt-get install -y"; req $SUDO apt-get update; req $INSTALL pipx playerctl dunst wireplumber curl; opt cava
elif command -v dnf     >/dev/null; then INSTALL="$SUDO dnf install -y";     req $INSTALL pipx playerctl dunst wireplumber curl; opt cava
elif command -v pacman  >/dev/null; then INSTALL="$SUDO pacman -S --needed --noconfirm"; req $INSTALL python-pipx playerctl dunst wireplumber curl; opt cava
else echo "Gestionnaire de paquets non reconnu : installe à la main pipx, playerctl, dunst, wireplumber (wpctl), curl, cava (facultatif)."; fi

pipx ensurepath
pipx install --force "$SRC"
echo
echo "Terminé. Ouvre un nouveau terminal puis :  tdash --init   (crée la config)   puis   tdash -p -w"

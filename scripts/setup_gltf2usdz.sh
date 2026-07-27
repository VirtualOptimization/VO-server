#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${GLTF2USDZ_REPO_DIR:-$HOME/gltf2usdz.online}"
BUN_BIN="${BUN_BINARY:-$HOME/.bun/bin/bun}"

if ! command -v bun >/dev/null 2>&1 && [ ! -x "$BUN_BIN" ]; then
  echo "Installing Bun..."
  curl -fsSL https://bun.sh/install | bash
fi

if [ -x "$BUN_BIN" ]; then
  export PATH="$(dirname "$BUN_BIN"):$PATH"
fi

if ! command -v bun >/dev/null 2>&1; then
  echo "bun was not found after installation" >&2
  exit 1
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "Cloning gltf2usdz.online into $REPO_DIR..."
  git clone https://github.com/arthurrmp/gltf2usdz.online.git "$REPO_DIR"
else
  echo "Updating gltf2usdz.online in $REPO_DIR..."
  git -C "$REPO_DIR" pull --ff-only
fi

echo "Installing gltf2usdz.online dependencies..."
cd "$REPO_DIR"
bun install --frozen-lockfile

echo "gltf2usdz.online is ready at $REPO_DIR"

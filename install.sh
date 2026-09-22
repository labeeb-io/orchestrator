#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
CFG_DIR="${HOME}/.config/labeeb-controller"

mkdir -p "$BIN_DIR" "$CFG_DIR"
install -m 0755 "$ROOT/labeeb_controller.py" "$BIN_DIR/labeeb-controller"
if [[ ! -f "$CFG_DIR/config.toml" ]]; then
  install -m 0644 "$ROOT/config.toml" "$CFG_DIR/config.toml"
  echo "created $CFG_DIR/config.toml"
else
  echo "kept existing $CFG_DIR/config.toml"
fi

echo "installed $BIN_DIR/labeeb-controller"
echo "run: $BIN_DIR/labeeb-controller --config $CFG_DIR/config.toml doctor"

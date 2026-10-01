#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN_DIR=${HOME}/.local/bin

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "Docker is required" >&2; exit 1; }

mkdir -p "$BIN_DIR"
ln -sf "$ROOT/bin/agy-multiplex-isolated" "$BIN_DIR/agy-multiplex-isolated"
ln -sf "$ROOT/bin/account-login-ui" "$BIN_DIR/agy-account-login-ui"

printf '%s\n' "Installed command links in $BIN_DIR"
printf '%s\n' "  agy-multiplex-isolated"
printf '%s\n' "  agy-account-login-ui"
printf '%s\n' ""
printf '%s\n' "If $BIN_DIR is not on PATH, add:"
printf '%s\n' "  export PATH=\"$BIN_DIR:\$PATH\""
printf '%s\n' ""
printf '%s\n' "Next:"
printf '%s\n' "  agy-multiplex-isolated build-image"
printf '%s\n' "  agy-multiplex-isolated init"

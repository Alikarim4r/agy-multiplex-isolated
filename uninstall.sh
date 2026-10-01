#!/bin/sh
set -eu
BIN_DIR=${HOME}/.local/bin
rm -f "$BIN_DIR/agy-multiplex-isolated" "$BIN_DIR/agy-account-login-ui"
printf '%s\n' "Command links removed."
printf '%s\n' "Runtime data and Docker credential volumes were preserved intentionally."
printf '%s\n' "Data root: ${AGY_MULTIPLEX_HOME:-$HOME/.agy-multiplex-isolated}"

#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <user@host> [path/to/key.pem]"
  echo "  e.g. $0 ubuntu@ec2-1-2-3-4.compute.amazonaws.com ~/Downloads/yapsgg.pem"
  exit 1
fi

TARGET="$1"
KEY="${2:-}"

cd "$(dirname "$0")/.."

SSH=(ssh -o StrictHostKeyChecking=accept-new)
if [ -n "$KEY" ]; then
  SSH+=(-i "$KEY")
fi

echo ">> uploading tracked files to $TARGET:\$HOME/yapsgg-bot"
git archive --format=tar HEAD | "${SSH[@]}" "$TARGET" \
  'mkdir -p "$HOME/yapsgg-bot" && tar xf - -C "$HOME/yapsgg-bot"'

echo ">> running setup on $TARGET"
"${SSH[@]}" "$TARGET" \
  'sudo APP_DIR="$HOME/yapsgg-bot" RUN_USER="$(id -un)" bash "$HOME/yapsgg-bot/deploy/setup.sh"'

echo ">> done"

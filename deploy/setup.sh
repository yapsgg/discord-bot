#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:?APP_DIR is required}"
RUN_USER="${RUN_USER:-$(stat -c '%U' "$APP_DIR")}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[ -n "$RUN_HOME" ] || RUN_HOME="$(eval echo "~$RUN_USER")"
ENV_FILE=/etc/yapsgg-bot.env
UNIT=/etc/systemd/system/yapsgg-bot.service
OC_UNIT=/etc/systemd/system/opencode.service
WORKSPACE="${WORKSPACE:-$RUN_HOME/opencode-workspace}"

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip ca-certificates curl unzip ripgrep
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip ca-certificates curl unzip ripgrep
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip ca-certificates curl unzip ripgrep
fi

echo ">> creating virtualenv in $APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  touch "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

ensure_env() {
  if ! grep -q "^$1=" "$ENV_FILE"; then
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}

ensure_env DISCORD_TOKEN ""
ensure_env OPENROUTER_API_KEY ""
ensure_env AI_MODEL "deepseek/deepseek-v4-flash-0731:free"
ensure_env MAX_UPLOAD_MB "8"
ensure_env ENABLE_MESSAGE_CONTENT "false"
ensure_env OPENCODE_SERVER_URL "http://127.0.0.1:4096"
ensure_env OPENCODE_SERVER_PASSWORD ""
ensure_env OPENCODE_MODEL "deepseek/deepseek-v4.1-flash"

OPENCODE_BIN="$RUN_HOME/.opencode/bin/opencode"
if [ ! -x "$OPENCODE_BIN" ]; then
  echo ">> installing opencode for $RUN_USER"
  sudo -u "$RUN_USER" -H bash -c 'curl -fsSL https://opencode.ai/install | bash' || true
fi
for candidate in \
  "$RUN_HOME/.opencode/bin/opencode" \
  "$RUN_HOME/.local/bin/opencode" \
  "/usr/local/bin/opencode"; do
  if [ -x "$candidate" ]; then
    OPENCODE_BIN="$candidate"
    break
  fi
done

echo ">> writing opencode config for $RUN_USER"
mkdir -p "$RUN_HOME/.config/opencode" "$WORKSPACE"
install -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" -m 644 \
  "$APP_DIR/deploy/opencode.json" "$RUN_HOME/.config/opencode/opencode.json"
chown -R "$RUN_USER" "$WORKSPACE"

sed -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__RUN_USER__|$RUN_USER|g" \
    -e "s|__ENV_FILE__|$ENV_FILE|g" \
    "$APP_DIR/deploy/yapsgg-bot.service" > "$UNIT"

sed -e "s|__RUN_USER__|$RUN_USER|g" \
    -e "s|__WORKSPACE__|$WORKSPACE|g" \
    -e "s|__ENV_FILE__|$ENV_FILE|g" \
    -e "s|__OPENCODE_BIN__|$OPENCODE_BIN|g" \
    "$APP_DIR/deploy/opencode.service" > "$OC_UNIT"

systemctl daemon-reload
systemctl enable opencode >/dev/null
systemctl restart opencode
systemctl enable yapsgg-bot >/dev/null

if grep -q '^DISCORD_TOKEN=.\+' "$ENV_FILE"; then
  systemctl restart yapsgg-bot
else
  echo ">> DISCORD_TOKEN is empty in $ENV_FILE; bot not started yet."
  echo "   set your secrets, then: sudo systemctl restart yapsgg-bot"
fi

sleep 2
echo "--- opencode ---"
systemctl --no-pager --full status opencode || true
echo "--- yapsgg-bot ---"
systemctl --no-pager --full status yapsgg-bot || true

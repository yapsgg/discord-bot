#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:?APP_DIR is required}"
RUN_USER="${RUN_USER:-$(stat -c '%U' "$APP_DIR")}"
ENV_FILE=/etc/yapsgg-bot.env
UNIT=/etc/systemd/system/yapsgg-bot.service

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip ca-certificates
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip ca-certificates
fi

echo ">> creating virtualenv in $APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
DISCORD_TOKEN=
OPENROUTER_API_KEY=
AI_MODEL=deepseek/deepseek-v4-flash-0731:free
MAX_UPLOAD_MB=8
ENABLE_MESSAGE_CONTENT=false
EOF
  chmod 600 "$ENV_FILE"
  echo ">> created $ENV_FILE"
  echo "   fill in your secrets, then: sudo systemctl restart yapsgg-bot"
fi

sed -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__RUN_USER__|$RUN_USER|g" \
    -e "s|__ENV_FILE__|$ENV_FILE|g" \
    "$APP_DIR/deploy/yapsgg-bot.service" > "$UNIT"

systemctl daemon-reload
systemctl enable yapsgg-bot >/dev/null
systemctl restart yapsgg-bot
sleep 2
systemctl --no-pager --full status yapsgg-bot || true

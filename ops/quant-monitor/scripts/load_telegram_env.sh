#!/usr/bin/env bash
# Load QuantSentinel Telegram credentials from GCP Secret Manager into a runtime env file.
set -euo pipefail

OUT="${1:-/run/quant-monitor/telegram.env}"
SECRET_NAME="${QUANT_SENTINEL_TELEGRAM_SECRET_NAME:-quant-sentinel-telegram-bot-token}"
GCP_PROJECT="${QUANT_SENTINEL_GCP_PROJECT:-}"
CHAT_ID="${GLOBAL_TELEGRAM_CHAT_ID:-}"

if [[ -z "$GCP_PROJECT" ]]; then
  echo "load_telegram_env: QUANT_SENTINEL_GCP_PROJECT (or GCP_PROJECT) required" >&2
  exit 1
fi
if [[ -z "$CHAT_ID" ]]; then
  echo "load_telegram_env: GLOBAL_TELEGRAM_CHAT_ID required (do not commit to git)" >&2
  exit 1
fi

install -d -m 0750 "$(dirname "$OUT")"
TOKEN="$(
  gcloud secrets versions access latest \
    --secret "$SECRET_NAME" \
    --project="$GCP_PROJECT" 2>/dev/null | tr -d '\n'
)"
if [[ -z "$TOKEN" ]]; then
  echo "load_telegram_env: failed to read GCP secret name=$SECRET_NAME (project=$GCP_PROJECT)" >&2
  exit 1
fi
if ! curl -fsS "https://api.telegram.org/bot${TOKEN}/getMe" | grep -q '"ok":true'; then
  echo "load_telegram_env: token in $SECRET_NAME failed Telegram getMe (project=$GCP_PROJECT)" >&2
  exit 1
fi
umask 0177
cat >"$OUT" <<EOF
TELEGRAM_TOKEN=$TOKEN
GLOBAL_TELEGRAM_CHAT_ID=$CHAT_ID
EOF
echo "[load_telegram_env] wrote $OUT (name=$SECRET_NAME project=$GCP_PROJECT)"

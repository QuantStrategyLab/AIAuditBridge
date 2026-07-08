#!/usr/bin/env bash
set -euo pipefail
ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=scripts/source_telegram_env.sh
source "$ROOT/scripts/source_telegram_env.sh" 2>/dev/null || true
DAY=$(date -u +%Y-%m-%d)
OUT="$ROOT/data/daily-reports/$DAY"
mkdir -p "$OUT"
for domain in cn_equity hk_equity us_equity crypto; do
  file="$OUT/${domain}.json"
  if command -v quant-lifecycle >/dev/null 2>&1; then
    quant-lifecycle dashboard --domain "$domain" --format json > "$file" 2>"$OUT/${domain}.err" || {
      printf '{"domain":"%s","ok":false,"error":"dashboard_failed"}\n' "$domain" > "$file"
    }
  else
    printf '{"domain":"%s","ok":false,"error":"quant-lifecycle_missing","as_of":"%s"}\n' "$domain" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$file"
  fi
  echo "[briefing] wrote $file"
done

# Lightweight local consumption (AIAuditBridge-compatible JSON shape)
python3 - <<'PY'
import json, os
from pathlib import Path
day = os.environ.get("DAY") or __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
root = Path(os.environ.get("QUANT_MONITOR_ROOT") or ".")
out = root / "data" / "daily-reports" / day
alerts = []
for path in sorted(out.glob("*.json")):
    if path.name.endswith(".err"):
        continue
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        alerts.append({"file": path.name, "level": "error", "reason": str(exc)})
        continue
    if data.get("ok") is False:
        alerts.append({"file": path.name, "level": "warn", "reason": data.get("error") or "ok=false"})
summary = {"day": day, "alerts": alerts, "report_dir": str(out)}
(out / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(summary, ensure_ascii=False))
# Telegram only for explicit warn flag; critical path uses AIAuditBridge --dispatch
if alerts and os.environ.get("BRIEFING_TELEGRAM_ON_WARN") == "1":
    token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TG_TOKEN")
    chat = os.environ.get("GLOBAL_TELEGRAM_CHAT_ID")
    if token and chat:
        from quant_platform_kit.notifications.telegram import send_telegram_message
        send_telegram_message(
            bot_token=token,
            chat_ids=chat,
            text=f"📊 daily briefing alerts ({day}): {len(alerts)}",
        )
PY

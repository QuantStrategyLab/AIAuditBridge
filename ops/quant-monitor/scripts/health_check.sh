#!/usr/bin/env bash
set -euo pipefail
ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=scripts/source_telegram_env.sh
source "$ROOT/scripts/source_telegram_env.sh" 2>/dev/null || true
OUT_DIR="$ROOT/data/health"
mkdir -p "$OUT_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
SUMMARY="$OUT_DIR/summary_$TS.json"

python3 - <<'PY' > "$SUMMARY"
import json, os, subprocess, sys
from datetime import datetime, timezone

domains = ["cn_equity", "hk_equity", "us_equity", "crypto"]
rows = []
for domain in domains:
    try:
        proc = subprocess.run(
            ["quant-lifecycle", "dashboard", "--domain", domain, "--format", "summary"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        rows.append({
            "domain": domain,
            "ok": proc.returncode == 0,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-1000:],
        })
    except FileNotFoundError:
        rows.append({"domain": domain, "ok": False, "error": "quant-lifecycle not installed"})
    except Exception as exc:
        rows.append({"domain": domain, "ok": False, "error": str(exc)})

payload = {
    "as_of": datetime.now(timezone.utc).isoformat(),
    "domains": rows,
    "alerts": [r for r in rows if not r.get("ok")],
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
sys.exit(0 if not payload["alerts"] else 2)
PY
rc=$?

python3 - <<'PY' || true
import json, os, sys
from pathlib import Path
summary_path = Path(os.environ.get("SUMMARY_PATH", ""))
# fall through: health_check uses latest summary
PY

if [[ -n "${TELEGRAM_TOKEN:-}" && -n "${GLOBAL_TELEGRAM_CHAT_ID:-}" && $rc -ne 0 ]]; then
  python3 - <<'PY'
import os
from quant_platform_kit.notifications.telegram import send_telegram_message
send_telegram_message(
    bot_token=os.environ["TELEGRAM_TOKEN"],
    chat_ids=os.environ["GLOBAL_TELEGRAM_CHAT_ID"],
    text="🚨 quant-monitor health_check: 有 domain dashboard 失败，请检查 VPS 日志",
)
print("[health] telegram alert sent")
PY
fi
exit $rc

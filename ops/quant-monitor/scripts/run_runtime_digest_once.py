#!/usr/bin/env python3
"""Run the existing paper runtime consumer once; never run the domain/AI pipeline.

Operator inputs come from the protected maintenance job. The one approved route
is the original QuantSentinel Secret Manager reference and global chat config.
No credential/body copy or new delivery store is created. Source identity is
checked by the workflow; the child imports only that exact application checkout.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pwd
from pathlib import Path
import re
import shlex
import subprocess
import sys
import urllib.request

SOURCE_SHA = "823ba856af49d8799506afe476aec9d07ab1c63d"
MONITOR_ROOT = Path("/home/ubuntu/quant-monitor-runtime/AIAuditBridge/ops/quant-monitor")
GCLOUD = "/usr/bin/gcloud"
SECRET_NAME = "quant-sentinel-telegram-bot-token"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _execution_home() -> str:
    identity = pwd.getpwuid(os.geteuid())
    home = os.environ.get("HOME", "")
    if identity.pw_name != "ubuntu" or home != identity.pw_dir:
        raise ValueError("execution identity unavailable")
    return home


def _route_config() -> tuple[str, str]:
    # Read the original protected unit's loader inputs, never print Environment
    # or source arbitrary shell/env files. This is the unique approved route.
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", "codex-daily-briefing.service", "--property=Environment", "--value"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    assignments = shlex.split(result.stdout.decode("utf-8"))
    config = {}
    for item in assignments:
        key, separator, value = item.partition("=")
        if not separator or key in config:
            raise ValueError("route unavailable")
        config[key] = value
        if value and ("TELEGRAM" in key or key.startswith("TG_")) and ("TOPIC" in key or "THREAD" in key):
            raise ValueError("topic unsupported")
    if config.get("QUANT_SENTINEL_TELEGRAM_SECRET_NAME", SECRET_NAME) != SECRET_NAME:
        raise ValueError("route unavailable")
    declared = config.get("QUANT_MONITOR_ROOT", "")
    data = MONITOR_ROOT / "data"
    if (not declared or not data.is_dir()
            or (Path(declared) / "data").resolve(strict=True) != data.resolve(strict=True)):
        raise ValueError("ledger alias unavailable")
    if any(config.get(key) for key in ("CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE")):
        raise ValueError("client storage requires explicit binding")
    return config.get("QUANT_SENTINEL_GCP_PROJECT", ""), config.get("GLOBAL_TELEGRAM_CHAT_ID", "")


def _credentials() -> tuple[str, str]:
    # Reuse load_telegram_env.sh's exact Secret Manager/getMe contract, but keep
    # stdout in memory instead of writing its runtime env file or sourcing .env.
    home = _execution_home()
    project, chat = _route_config()
    if (not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project)
            or not re.fullmatch(r"-?[0-9]+", chat)):
        raise ValueError("route unavailable")
    result = subprocess.run(
        [GCLOUD, "secrets", "versions", "access", "latest", "--secret", SECRET_NAME, "--project", project],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "C", "LC_ALL": "C"},
    )
    token = result.stdout.decode("utf-8").strip()
    if len(token) > 8192 or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("route unavailable")
    request = urllib.request.Request("https://api.telegram.org/bot" + token + "/getMe")
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        body = json.loads(response.read(8193))
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise ValueError("route unavailable")
    return token, chat


def _validate(source_root: Path, uri: str, day: str, targets: list[str]) -> None:
    sys.path.insert(0, str(source_root))
    from scripts import consume_daily_briefing as cli
    if (cli._runtime_object_problem(uri, day) is not None
            or cli._paper_scope_problem(targets) is not None
            or cli._runtime_object_observed_at(uri) is None):
        raise ValueError("input unavailable")


def _arguments(source_root: Path, mode: str, uri: str, day: str, targets: list[str]) -> list[str]:
    # Fresh child process for every invocation; no ambient PYTHONPATH or imports.
    return ["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()),
            "--consumer-child", "--source-root", str(source_root), "--mode", mode,
            "--object", uri, "--day", day,
            *[item for key in targets for item in ("--expected-target-key", key)]]


def _child(source_root: Path, mode: str, uri: str, day: str, targets: list[str]) -> dict:
    sys.path.insert(0, str(source_root))
    from scripts import consume_daily_briefing as cli
    import importlib
    dispatch = importlib.import_module(cli.dispatch_runtime_digest.__module__)
    health = dispatch._health_cycle_module()
    # Existing persistent state must be readable and structurally valid. Do not
    # create an empty ledger to make a missing history look like a new event.
    state = MONITOR_ROOT / "data/alert-state/health_cycle.json"
    if (not state.is_file() or not os.access(state, os.R_OK)
            or not os.access(state.parent, os.W_OK | os.X_OK)):
        raise ValueError("ledger unavailable")
    health._load_delivery_payload(MONITOR_ROOT)
    token, chat = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("GLOBAL_TELEGRAM_CHAT_ID", "")
    if not token or dispatch._telegram_token() != token or dispatch._telegram_chat_ids() != (chat,):
        raise ValueError("route unavailable")
    args = ["--runtime-projection-gcs", uri, "--day", day,
            *[item for key in targets for item in ("--expected-target-key", key)]]
    if mode == "send":
        args.append("--dispatch")
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(args)
    body = json.loads(output.getvalue())
    summary = body.get("dispatch", {})
    valid = code == 0 and body.get("ok") is True and body.get("kind") == "runtime_digest"
    sent = valid and summary.get("telegram_sent") is True and summary.get("errors") == []
    # The original dispatcher deliberately reports telegram_sent=False for a
    # suppressed duplicate. Confirm historical sent records, not just its skip
    # flag (a legacy fingerprint without per-target evidence also suppresses).
    current = health._load_delivery_payload(MONITOR_ROOT)
    record = current.get("deliveries", {}).get(body.get("event_id"), {}).get(health._delivery_target_hash(chat), {})
    duplicate = (valid and summary.get("errors") == []
                 and "duplicate_delivered" in summary.get("skipped", [])
                 and record.get("status") == "sent")
    return {"ok": bool(valid), "approved_route_bound": True, "ledger_readable": True,
            "sent": bool(sent), "duplicate": bool(duplicate)}


def _invoke(source_root: Path, mode: str, uri: str, day: str, targets: list[str], token: str, chat: str) -> dict:
    env = {"PATH": str(Path(GCLOUD).parent) + ":/usr/bin:/bin", "HOME": _execution_home(),
           "LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
           "TELEGRAM_TOKEN": token, "GLOBAL_TELEGRAM_CHAT_ID": chat,
           "QUANT_MONITOR_ROOT": str(MONITOR_ROOT)}
    result = subprocess.run(_arguments(source_root, mode, uri, day, targets),
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=90, check=False)
    if result.returncode != 0:
        raise ValueError("consumer incomplete")
    body = json.loads(result.stdout)
    if (not isinstance(body, dict) or set(body) != {"ok", "approved_route_bound", "ledger_readable", "sent", "duplicate"}
            or any(type(value) is not bool for value in body.values())):
        raise ValueError("consumer incomplete")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("preview", "send"), default="preview")
    parser.add_argument("--object", required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--expected-target-key", action="append", required=True)
    parser.add_argument("--consumer-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # No dynamic shell evaluation; all errors and exception details stay private.
    try:
        if args.consumer_child:
            outcome = _child(args.source_root, args.mode, args.object, args.day, args.expected_target_key)
            print(json.dumps(outcome, sort_keys=True))
            return 0 if outcome["ok"] else 2
        _validate(args.source_root, args.object, args.day, args.expected_target_key)
        token, chat = _credentials()
        first = _invoke(args.source_root, args.mode, args.object, args.day, args.expected_target_key, token, chat)
        if not first["ok"] or (args.mode == "send" and not (first["sent"] or first["duplicate"])):
            raise ValueError("consumer incomplete")
        result = {"ok": True, "mode": args.mode, "approved_route_bound": first["approved_route_bound"],
                  "ledger_readable": first["ledger_readable"], "sent": first["sent"],
                  "already_delivered": first["duplicate"], "restart_duplicate": False}
        if args.mode == "send":
            # Only known terminal success permits the second fresh process.
            # Timeout/unknown/return loss never trigger an automatic retry.
            second = _invoke(args.source_root, "send", args.object, args.day, args.expected_target_key, token, chat)
            result["restart_duplicate"] = second["ok"] and second["duplicate"]
            result["ok"] = bool(result["restart_duplicate"])
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 2
    except Exception:
        print('{"ok": false, "status": "stopped_without_retry"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

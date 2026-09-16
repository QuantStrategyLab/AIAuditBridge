"""Run the four fixed semantic-quality research prompts once, in order."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable

from client.config import GatewayConfig
from client.gateway_client import AiGatewayClient
from service.research_diagnosis import build_research_diagnosis_prompt, build_research_diagnosis_request
from tests.test_research_diagnosis import _semantic_quality_triggers, _task

MAX_OUTPUT_CHARS = 12_000
MODEL = "gpt-5.6-terra"
REASONING_EFFORT = "medium"
SECTION_TITLES = (
    "## 已验证事实",
    "## 可检验假设",
    "## 下一轮离线研究",
    "## 边界与升级条件",
)


def _write_summary(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _safe_result_metadata(result: Any) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key in ("job_id", "status", "provider", "research_stage", "reasoning_effort"):
        value = getattr(result, key, None)
        if value is None and isinstance(getattr(result, "raw", None), dict):
            value = result.raw.get(key)
        if isinstance(value, (str, int, float, bool)):
            metadata["result_status" if key == "status" else key] = str(value)
    model = getattr(result, "model", None)
    if isinstance(model, str) and model:
        metadata["model"] = model
    return metadata


def run_acceptance(
    output_path: Path,
    *,
    config: GatewayConfig | None = None,
    client_factory: Callable[[GatewayConfig], AiGatewayClient] = AiGatewayClient,
) -> dict[str, Any]:
    """Call each fixed case once; stop after the first unavailable result."""
    task = _task()
    triggers = _semantic_quality_triggers()
    if len(triggers) != 4:
        raise ValueError("semantic quality acceptance requires exactly four fixed triggers")
    client = client_factory(config or GatewayConfig.from_env())
    cases: list[dict[str, Any]] = []
    stopped = False
    for trigger in triggers:
        case: dict[str, Any] = {"kind": str(trigger["kind"]), "status": "deferred"}
        if stopped:
            case["reason"] = "stopped_after_failure"
            cases.append(case)
            continue
        try:
            request = build_research_diagnosis_request(task, trigger=trigger)
            result = client.execute(
                build_research_diagnosis_prompt(request),
                task="execute",
                mode="review_only",
                model=MODEL,
                research_stage="drift_analysis",
                reasoning_effort=REASONING_EFFORT,
                sandbox="read-only",
                allowed_providers=["codex"],
                source_repository=str(request["target"]["repository"]),
                source_ref=str(request["target"]["strategy_revision"]),
                timeout=600,
            )
        except Exception:  # provider details must not enter the artifact
            stopped = True
            case["reason"] = "call_failed"
            cases.append(case)
            continue
        metadata = _safe_result_metadata(result)
        output = getattr(result, "output", None)
        if not getattr(result, "success", False) or getattr(result, "provider", "") != "codex" or not isinstance(output, str) or not output.strip():
            stopped = True
            case["reason"] = "call_failed"
            case.update(metadata)
            cases.append(case)
            continue
        if len(output) > MAX_OUTPUT_CHARS:
            stopped = True
            case["reason"] = "output_too_long"
            case.update(metadata)
            cases.append(case)
            continue
        positions = [output.find(title) for title in SECTION_TITLES]
        if any(position < 0 for position in positions) or positions != sorted(positions):
            stopped = True
            case["reason"] = "format_invalid"
            case["output"] = output
            case.update(metadata)
            cases.append(case)
            continue
        case.update(
            status="completed",
            task_id=str(request["task_id"]),
            task_sha256=str(request["task_sha256"]),
            output=output,
        )
        case.update(metadata)
        cases.append(case)
    summary = {
        "status": "deferred" if stopped else "completed",
        "review_required": True,
        "human_reviewed": False,
        "source_revision": os.environ.get("GITHUB_SHA", ""),
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "research_stage": "drift_analysis",
        "sandbox": "read-only",
        "provider": "codex",
        "section_titles": list(SECTION_TITLES),
        "cases": cases,
    }
    return _write_summary(output_path, summary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_acceptance(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

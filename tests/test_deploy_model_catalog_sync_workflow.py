from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github/workflows/deploy_model_catalog_sync.yml"
)
CHECKOUT_PIN = "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3"


def top_level_block(workflow: str, key: str) -> str:
    lines = workflow.splitlines()
    start = lines.index(f"{key}:")
    block = [lines[start]]

    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "\t")):
            break
        block.append(line)

    return "\n".join(block)


def assert_permissions_boundary(workflow: str) -> None:
    assert top_level_block(workflow, "permissions") == "permissions:\n  contents: read\n"


def workflow_uses(workflow: str) -> list[str]:
    return [
        match.group(1).strip()
        for line in workflow.splitlines()
        if (match := re.match(r"^\s*-?\s*uses:\s*(.+)$", line))
    ]


def test_deploy_model_catalog_sync_keeps_self_hosted_secret_boundary_and_pins_checkout() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "runs-on:\n      - self-hosted\n      - codex-vps" in workflow
    assert_permissions_boundary(workflow)
    assert workflow_uses(workflow) == [CHECKOUT_PIN]
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in workflow
    assert "ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}" in workflow


def test_deploy_model_catalog_sync_is_manual_only_and_keeps_inspect_mode() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    triggers = top_level_block(workflow, "on")

    assert "workflow_dispatch:" in triggers
    assert "          - inspect" in triggers
    assert "\n  push:" not in triggers


def test_permissions_boundary_rejects_added_write_permission() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    weakened = workflow.replace("permissions:\n", "permissions:\n  actions: write\n", 1)

    with pytest.raises(AssertionError):
        assert_permissions_boundary(weakened)


def test_checkout_pin_rejects_an_additional_unpinned_checkout() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    weakened = f"{workflow}\n      - uses: actions/checkout@main\n"

    with pytest.raises(AssertionError):
        assert workflow_uses(weakened) == [CHECKOUT_PIN]

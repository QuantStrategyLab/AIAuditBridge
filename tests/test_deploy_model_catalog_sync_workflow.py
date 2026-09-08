from __future__ import annotations

import re
import os
import subprocess
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


def test_workflow_installs_its_exact_checkout_without_updating_the_user_repository() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert 'AIAUDIT_BRIDGE_ROOT: ${{ github.workspace }}' in workflow
    assert 'MODEL_CATALOG_REVISION: ${{ github.sha }}' in workflow
    assert 'git -C' not in workflow and 'pull --ff-only' not in workflow
    assert '/home/ubuntu/Projects/AIAuditBridge' not in workflow


def test_release_install_uses_committed_code_and_preserves_dirty_checkout_and_existing_config(tmp_path):
    root = WORKFLOW_PATH.parents[2]
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--quiet", "--shared", str(root), str(checkout)], check=True)
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    documents = [checkout / "ops/quant-monitor/AGENTS.md", checkout / "ops/quant-monitor/README.md"]
    for document in documents:
        document.write_text("private-user-change\n")
    (checkout / "untracked-secret").write_text("private-marker")
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    config = config_dir / "model-catalog.env"
    config.write_text("EXISTING_CONFIG=private-marker\n")
    config.chmod(0o600)
    releases = tmp_path / "releases"
    env = {**os.environ, "AIAUDIT_BRIDGE_ROOT": str(checkout), "MODEL_CATALOG_REVISION": revision,
           "MODEL_CATALOG_RELEASE_ROOT": str(releases), "TEST_ENV_FILE": str(config)}
    script = root / "ops/codex-audit/scripts/deploy_model_catalog_sync.sh"
    command = 'source "$1"; sudo() { "$@"; }; ENV_FILE="$TEST_ENV_FILE"; stage_bridge_release; write_env_file; stage_bridge_release'
    result = subprocess.run(["bash", "-c", command, "fixture", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    release = releases / revision
    assert (release / "scripts/sync_model_catalog.py").is_file()
    assert (release / "service/model_catalog.py").read_bytes() == (checkout / "service/model_catalog.py").read_bytes()
    assert not (release / "ops/quant-monitor").exists() and not (release / "untracked-secret").exists()
    assert list(releases.iterdir()) == [release]
    assert all(document.read_text() == "private-user-change\n" for document in documents)
    assert config.read_text() == "EXISTING_CONFIG=private-marker\n"
    assert config.stat().st_mode & 0o777 == 0o600 and config_dir.stat().st_mode & 0o777 == 0o700
    assert "private-marker" not in result.stdout + result.stderr
    # Do not silently replace an existing release or accept a moving/default ref.
    frozen_code = release / "service/model_catalog.py"
    frozen_code.write_text("private-tamper\n")
    rejected = subprocess.run(["bash", "-c", command, "fixture", str(script)], env=env, capture_output=True, text=True)
    assert rejected.returncode != 0 and "refusing overwrite" in rejected.stderr
    assert frozen_code.read_text() == "private-tamper\n"
    assert "private-tamper" not in rejected.stdout + rejected.stderr
    env["MODEL_CATALOG_REVISION"] = "main"
    invalid = subprocess.run(["bash", "-c", command, "fixture", str(script)], env=env, capture_output=True, text=True)
    assert invalid.returncode != 0 and "exact checkout revision" in invalid.stderr
    assert list(releases.iterdir()) == [release]


def test_model_catalog_service_uses_installed_release_and_keeps_monthly_schedule():
    root = WORKFLOW_PATH.parents[2]
    service = (root / "ops/codex-audit/systemd/model-catalog-sync.service.example").read_text()
    timer = (root / "ops/codex-audit/systemd/model-catalog-sync.timer.example").read_text()
    assert "@MODEL_CATALOG_CODE_ROOT@" in service and "/home/ubuntu/Projects" not in service
    assert "OnCalendar=*-*-01 06:00:00" in timer

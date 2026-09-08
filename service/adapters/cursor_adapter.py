"""Bounded Cursor ask-mode execution in a disposable, service-owned workspace."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from service.adapters.codex_adapter import CodexResult, _codex_env

_WORKSPACE = Path(__file__).resolve().parents[2] / 'ops/cursor-research/workspace'
_DENY = ['Shell(*)', 'Write(**)', 'Write(/**)', 'Mcp(*:*)', 'WebFetch(*)', 'Read(**)', 'Read(/**)', 'Read(../**)']


class CursorAdapter:
    def execute(self, *, prompt, sandbox='read-only', model=None, reasoning_effort=None, timeout=2700):
        if sandbox != 'read-only' or reasoning_effort not in {'low', 'medium', 'high', 'xhigh'} or not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}', model):
            return CodexResult(success=False, error='cursor configuration invalid')
        executable = shutil.which(os.environ.get('AI_GATEWAY_CURSOR_BIN', 'agent'))
        if not executable:
            return CodexResult(success=False, error='cursor command not found')
        try:
            with tempfile.TemporaryDirectory(prefix='aab-cursor-') as directory:
                workspace = Path(directory)
                shutil.copytree(_WORKSPACE, workspace, dirs_exist_ok=True)
                (workspace / '.cursor').mkdir()
                (workspace / '.cursor/cli.json').write_text(json.dumps({'permissions': {
                    'allow': [], 'deny': _DENY,
                }}))
                env = {k: v for k, v in _codex_env().items() if not k.startswith(('AI_GATEWAY_', 'CURSOR_'))}
                instructions = '\n\n'.join(path.read_text() for path in (
                    workspace / 'AGENTS.md',
                    workspace / '.agents/skills/research-evidence/SKILL.md',
                    workspace / '.agents/skills/diagnosis-validation/SKILL.md',
                ))
                completed = subprocess.run([
                    executable, '--disable-auto-update', '--print', '--output-format', 'json', '--mode', 'ask',
                    '--allowed-tools', '', '--exclude-workspace-context',
                    '--sandbox', 'enabled', '--workspace', directory,
                    '--model', model,
                ], input=instructions + '\n\nTask and supplied evidence:\n' + prompt, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=directory, timeout=timeout, check=False, env=env)
        except subprocess.TimeoutExpired:
            return CodexResult(success=False, error='cursor execution timed out; outcome unknown')
        except (OSError, ValueError):
            return CodexResult(success=False, error='cursor execution unavailable')
        if completed.returncode != 0:
            return CodexResult(success=False, error='cursor execution failed')
        try:
            result = json.loads(completed.stdout)
        except (TypeError, ValueError):
            return CodexResult(success=False, error='cursor output contract invalid')
        if (not isinstance(result, dict) or result.get('type') != 'result' or result.get('subtype') != 'success'
            or result.get('is_error') is not False or not isinstance(result.get('result'), str) or not result['result'].strip()):
            return CodexResult(success=False, error='cursor output contract invalid')
        return CodexResult(success=True, output=result['result'])

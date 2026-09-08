import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from service.adapters.cursor_adapter import CursorAdapter


@pytest.mark.parametrize('payload', [None, {}, {'type': 'assistant', 'result': 'text'},
    {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'unsafe'},
    {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': ''}])
def test_missing_valid_terminal_result_fails_without_diagnostic_leak(payload):
    with patch('service.adapters.cursor_adapter.shutil.which', return_value='/synthetic/agent'), patch(
        'service.adapters.cursor_adapter.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='private-marker')
    ):
        result = CursorAdapter().execute(prompt='synthetic', model='synthetic-model', reasoning_effort='high')
    assert not result.success
    assert not result.output
    assert 'private-marker' not in repr(result)


def test_runner_uses_readonly_workspace_no_privileged_flags_or_secret_env():
    def run(command, **kwargs):
        assert command[command.index('--mode') + 1] == 'ask'
        assert command[command.index('--sandbox') + 1] == 'enabled'
        assert command[command.index('--model') + 1] == 'synthetic-model'
        assert command[command.index('--allowed-tools') + 1] == ''
        assert '--exclude-workspace-context' in command
        assert '--disable-auto-update' in command
        assert not {'--force', '--yolo', '--approve-mcps', '--api-key'} & set(command)
        assert kwargs['input'].endswith('Task and supplied evidence:\nsynthetic prompt')
        assert 'OPENAI_API_KEY' not in kwargs['env']
        assert 'CURSOR_API_KEY' not in kwargs['env']
        workspace = Path(kwargs['cwd'])
        assert workspace.joinpath('AGENTS.md').is_file()
        assert workspace.joinpath('.agents/skills/research-evidence/SKILL.md').is_file()
        assert 'Shell(*)' in json.loads(workspace.joinpath('.cursor/cli.json').read_text())['permissions']['deny']
        return SimpleNamespace(returncode=0, stdout=json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'advisory'}), stderr='')
    with patch('service.adapters.cursor_adapter.shutil.which', return_value='/synthetic/agent'), patch.dict(
        'os.environ', {'OPENAI_API_KEY': 'synthetic-private', 'CURSOR_API_KEY': 'synthetic-private'}
    ), patch('service.adapters.cursor_adapter.subprocess.run', side_effect=run) as runner:
        result = CursorAdapter().execute(prompt='synthetic prompt', model='synthetic-model', reasoning_effort='high')
    assert result.success and result.output == 'advisory'
    assert runner.call_count == 1


def test_timeout_is_unknown_and_never_retried():
    with patch('service.adapters.cursor_adapter.shutil.which', return_value='/synthetic/agent'), patch(
        'service.adapters.cursor_adapter.subprocess.run', side_effect=subprocess.TimeoutExpired(['private-marker'], 1)
    ) as runner:
        result = CursorAdapter().execute(prompt='synthetic', model='synthetic-model', reasoning_effort='high')
    assert not result.success
    assert 'unknown' in result.error
    assert 'private-marker' not in repr(result)
    runner.assert_called_once()

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


def test_headless_start_trusts_only_each_new_service_owned_workspace(tmp_path, monkeypatch):
    external = tmp_path / 'external-repository'
    external.mkdir()
    (external / 'private.txt').write_text('must not enter the task workspace')
    monkeypatch.chdir(external)
    workspaces = []

    def headless_cli(command, **kwargs):
        workspace = Path(command[command.index('--workspace') + 1])
        workspaces.append(workspace)
        assert workspace == Path(kwargs['cwd']) and workspace.is_dir()
        assert workspace.name.startswith('aab-cursor-')
        assert workspace != external and external not in workspace.parents
        assert workspace != Path(__file__).resolve().parents[1]
        assert command.count('--workspace') == 1
        assert str(workspace.parent) not in command and str(external) not in command
        assert not workspace.joinpath('private.txt').exists()
        assert workspace.joinpath('AGENTS.md').is_file()
        assert not {'--force', '--yolo', '--approve-mcps'} & set(command)
        if '--trust' not in command:
            # Headless CLI refuses a fresh workspace before producing a result.
            return SimpleNamespace(returncode=1, stdout='', stderr='workspace trust required')
        assert command.count('--trust') == 1
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'synthetic advisory',
        }), stderr='')

    with patch('service.adapters.cursor_adapter.shutil.which', return_value='/synthetic/agent'), patch(
        'service.adapters.cursor_adapter.subprocess.run', side_effect=headless_cli,
    ) as runner:
        for _ in range(2):
            result = CursorAdapter().execute(prompt=f'synthetic evidence about {external}', model='synthetic-model', reasoning_effort='high')
            assert result.success and result.output == 'synthetic advisory'
    assert runner.call_count == 2
    assert len(set(workspaces)) == 2
    assert all(not workspace.exists() for workspace in workspaces)
    assert (external / 'private.txt').read_text() == 'must not enter the task workspace'

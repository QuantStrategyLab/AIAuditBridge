from pathlib import Path


def test_repair_rehearsal_is_manual_artifact_only_and_excludes_other_jobs():
    workflow = Path('.github/workflows/codex_audit.yml').read_text()
    jobs = workflow.split('\njobs:\n', 1)[1]
    # Read complete job headers, stopping at runs-on rather than nested YAML fields.
    for name in ('codex-audit', 'synthetic-sdk-check', 'daily-summary',
                 'operational-diagnosis', 'historical-diagnosis-rehearsal',
                 'watchdog-repair-rehearsal'):
        header = jobs.split(f'  {name}:\n', 1)[1].split('    runs-on:', 1)[0]
        if name == 'watchdog-repair-rehearsal':
            assert "github.event_name == 'workflow_dispatch'" in header
            assert "github.ref == 'refs/heads/main'" in header
            assert 'inputs.watchdog_repair_rehearsal == true' in header
        else:
            assert 'inputs.watchdog_repair_rehearsal != true' in header
    rehearsal = jobs.split('  watchdog-repair-rehearsal:\n', 1)[1]
    assert 'persist-credentials: false' in rehearsal
    assert 'contents: read' in rehearsal
    assert 'id-token: write' in rehearsal
    assert 'AI_GATEWAY_RESEARCH_PROVIDERS: codex' in rehearsal
    assert 'scripts.run_watchdog_repair_rehearsal' in rehearsal
    assert 'actions/upload-artifact@' in rehearsal
    assert 'git push' not in rehearsal
    assert 'auto_merge' not in rehearsal
    assert 'create-github-app-token' not in rehearsal

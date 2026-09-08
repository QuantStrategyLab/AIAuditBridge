"""Run installer against temporary existing config, with systemd effects stubbed."""
from pathlib import Path
import stat
import subprocess


def test_installer_preserves_existing_configuration_directory_permissions(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / 'config'
    policy = tmp_path / 'policy'
    deploy = tmp_path / 'deploy'
    bindir = tmp_path / 'bin'
    for directory in (config, policy, deploy / 'service', bindir):
        directory.mkdir(parents=True)
    config.chmod(0o700)
    policy.chmod(0o750)
    (config / 'cursor.env').write_text('synthetic existing config')
    (policy / 'cursor_research.json').write_text('synthetic existing policy')
    (deploy / 'service/cursor_account.py').touch()
    ownership = [(path.stat().st_uid, path.stat().st_gid) for path in (config, policy)]
    # Only commands needed for existing temporary directories are executed.
    # No credential, root directory or systemd mutation reaches the host.
    sudo = bindir / 'sudo'
    sudo.write_text('''#!/bin/bash
if [[ "$1" == -n ]]; then exit 0; fi
case "$1" in
  test|install) exec "$@" ;;
  tee) cat >/dev/null ;;
  systemctl) exit 0 ;;
  *) exit 90 ;;
esac
''')
    sudo.chmod(0o755)
    source = (root / 'scripts/deploy_cursor_research.sh').read_text()
    source = source.replace('CURSOR_CONFIG_ROOT=/etc/codex-audit-bridge', f'CURSOR_CONFIG_ROOT={config}')
    source = source.replace('CURSOR_POLICY_ROOT=/etc/codex-audit-bridge-policy', f'CURSOR_POLICY_ROOT={policy}')
    script = tmp_path / 'install.sh'
    script.write_text(source)
    result = subprocess.run(['/bin/bash', str(script), 'install'], cwd=root, capture_output=True, text=True,
        env={'PATH': f'{bindir}:/usr/bin:/bin', 'AI_GATEWAY_CURSOR_BIN': '/usr/bin/true', 'CODEX_AUDIT_SERVICE_DEPLOY_DIR': str(deploy)})
    assert result.returncode == 0, result.stderr
    assert [stat.S_IMODE(path.stat().st_mode) for path in (config, policy)] == [0o700, 0o750]
    assert [(path.stat().st_uid, path.stat().st_gid) for path in (config, policy)] == ownership
    assert (config / 'cursor.env').read_text() == 'synthetic existing config'
    assert (policy / 'cursor_research.json').read_text() == 'synthetic existing policy'

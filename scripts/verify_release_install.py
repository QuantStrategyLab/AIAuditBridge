#!/usr/bin/env python3
"""Check the installed Quant wheel in both the build and offline bundle venvs."""
import argparse
import importlib
from importlib.metadata import distribution
import json
from pathlib import Path
import subprocess
import sys


def verify(expected_version):
    package = distribution('quant-ai-service')
    if package.version != expected_version:
        raise ValueError('installed Quant version differs from release version')
    modules = ['auth', 'client', 'events', 'gemini', 'inference', 'mcp_logging',
               'notifications', 'providers', 'runtime', 'server', 'transport', 'worker_tools',
               'scenarios', 'scenarios.instance_cli', 'scenarios.pilot',
               'scenarios.pilot_cli', 'scenarios.pilot_domain', 'scenarios.protocol_probe']
    for name in ['ai_service.' + name for name in modules] + ['quant_ai_service.server']:
        module = importlib.import_module(name)
        relative = name.replace('.', '/') + ('/__init__.py' if name == 'ai_service.scenarios' else '.py')
        if Path(module.__file__).resolve() != Path(package.locate_file(relative)).resolve():
            raise ValueError('runtime was imported from outside the installed wheel')
    for name in ['scenario-instance.v1.schema.json', 'scenario-task.v1.schema.json']:
        json.loads(Path(package.locate_file('ai_service/scenarios/' + name)).read_text())
    if 'AI service contributors' not in Path(package.locate_file('ai_service/LICENSE')).read_text():
        raise ValueError('adapted runtime license is missing')
    if any('personal-ai-service' in name.lower().replace('_', '-') for name in package.requires or []):
        raise ValueError('Quant must not depend on the Personal package')
    entries = {entry.name: entry.value for entry in package.entry_points if entry.group == 'console_scripts'}
    expected = {'quant-ai-service': 'quant_ai_service.server:main',
                'ai-task': 'ai_service.client:main',
                'ai-scenario-instance': 'ai_service.scenarios.instance_cli:main'}
    if entries != expected:
        raise ValueError('Quant release entry points differ from the supported commands')
    for name in expected:
        subprocess.run([str(Path(sys.executable).parent / name), '--help'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    subprocess.run([sys.executable, '-I', '-m', 'ai_service.scenarios.pilot_cli', '--help'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return {'version': package.version, 'installed_modules_checked': len(modules) + 1,
            'console_scripts_checked': len(expected), 'schemas_checked': 2,
            'personal_dependency': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.version), sort_keys=True))

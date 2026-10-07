"""Quant deployment binds project identities; strategy rules stay in callers."""
from ai_service.server import main as shared_main


def validate_configuration(config):
    projects = config.get('projects', {})
    if not projects or any(not name.startswith('QuantStrategyLab/') or len(name.split('/')) != 2
                           for name in projects):
        raise ValueError('Quant deployment admits explicitly configured QuantStrategyLab repositories only')
    allowed_scopes = {'tasks:read', 'tasks:submit', 'tasks:work', 'events:subscribe'}
    for grant in config.get('grants', []):
        if grant['project'] not in projects:
            raise ValueError('identity grant must belong to an admitted Quant project')
        if set(grant['scopes']) - allowed_scopes:
            raise ValueError('AI service grants do not authorize financial or deployment actions')


def main():
    shared_main(configuration_guard=validate_configuration)


if __name__ == '__main__':
    main()

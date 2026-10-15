"""Configured owner admission; business rules remain in caller repositories."""
from ai_service.server import main as runtime_main
from ai_service.auth import grant_projects, project_belongs_to, valid_owner


def validate_configuration(config):
    owner = config.get('owner')
    projects = config.get('projects', {})
    if not valid_owner(owner) or not projects or any(not project_belongs_to(name, owner) for name in projects):
        raise ValueError('configure one exact service owner and its admitted projects')
    allowed_scopes = {'tasks:read', 'tasks:submit', 'tasks:work', 'events:subscribe'}
    for grant in config.get('grants', []):
        explicit = grant_projects(grant)
        if grant.get('owner', owner) != owner or not explicit <= projects.keys():
            raise ValueError('identity grant must belong to an admitted service project or its exact owner')
        if set(grant['scopes']) - allowed_scopes:
            raise ValueError('AI service grants do not authorize financial or deployment actions')


def main():
    runtime_main(configuration_guard=validate_configuration)


if __name__ == '__main__':
    main()

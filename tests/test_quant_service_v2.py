"""Owner policy is configured, never inferred from a private organization name."""
import unittest
from quant_ai_service.server import validate_configuration


def config(grant=None):
    return {'owner': 'example-org', 'projects': {'example-org/research': {}, 'example-org/new': {}},
            'grants': [grant] if grant else []}


def worker(**changes):
    return {'issuer': 'https://issuer.example', 'subject': 'user', 'identity': 'worker',
            'owner': 'example-org', 'provider': 'dot', 'claims': {'azp': 'worker-client'},
            'scopes': ['tasks:read', 'tasks:work', 'events:subscribe'], **changes}


class QuantServiceConfigurationTests(unittest.TestCase):
    def test_new_repository_requires_no_worker_grant_edit(self):
        value = config(worker())
        validate_configuration(value)
        value['projects']['example-org/future'] = {}
        validate_configuration(value)

    def test_missing_wildcard_or_cross_owner_service_policy_is_rejected(self):
        for changes in [{'owner': None}, {'owner': '*'}, {'owner': 'example-org/*'},
                        {'projects': {'other-org/research': {}}}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_configuration({**config(), **changes})

    def test_cross_owner_or_submit_worker_scope_is_rejected(self):
        for changes in [{'owner': 'other-org'}, {'owner': '*'}, {'owner': '*/repo'},
                        {'owner': 'example-org/*'}, {'scopes': ['tasks:work', 'tasks:submit']},
                        {'issuer': 'https://token.actions.githubusercontent.com'}, {'claims': {}}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_configuration(config(worker(**changes)))

    def test_explicit_same_owner_list_and_single_project_compatibility(self):
        grant = worker()
        grant.pop('owner')
        grant['projects'] = ['example-org/research', 'example-org/new']
        validate_configuration(config(grant))
        grant.pop('projects')
        grant['project'] = 'example-org/research'
        validate_configuration(config(grant))
        grant['project'] = 'example-org/not-admitted'
        with self.assertRaises(ValueError):
            validate_configuration(config(grant))

    def test_list_rejects_cross_owner_wildcards_and_mixed_selectors(self):
        for projects in [['example-org/research', 'other-org/research'], ['example-org/*']]:
            grant = worker()
            grant.pop('owner')
            grant['projects'] = projects
            with self.assertRaises(ValueError):
                validate_configuration(config(grant))
        with self.assertRaises(ValueError):
            validate_configuration(config(worker(project='example-org/research')))

    def test_submitter_and_non_task_scopes_remain_separate(self):
        validate_configuration(config({'project': 'example-org/research', 'scopes': ['tasks:read', 'tasks:submit']}))
        with self.assertRaises(ValueError):
            validate_configuration(config({'project': 'example-org/research', 'scopes': ['trades:write']}))


if __name__ == '__main__':
    unittest.main()

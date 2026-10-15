import unittest
from quant_ai_service.server import validate_configuration


class QuantServiceConfigurationTests(unittest.TestCase):
    def test_arbitrary_quant_project_does_not_need_strategy_specific_service_code(self):
        validate_configuration({'projects': {'QuantStrategyLab/NewResearch': {}}, 'grants': [
            {'project': 'QuantStrategyLab/NewResearch', 'scopes': ['tasks:submit', 'tasks:read']}]})

    def test_personal_project_and_unadmitted_grant_are_rejected(self):
        for config in [{'projects': {'Pigbibi/PhotoStory': {}}},
                       {'projects': {'QuantStrategyLab/NewResearch': {}}, 'grants': [
                           {'project': 'QuantStrategyLab/Other', 'scopes': ['tasks:read']}]}]:
            with self.assertRaises(ValueError):
                validate_configuration(config)

    def test_ai_grants_cannot_authorize_trades_or_deployment(self):
        with self.assertRaises(ValueError):
            validate_configuration({'projects': {'QuantStrategyLab/NewResearch': {}}, 'grants': [
                {'project': 'QuantStrategyLab/NewResearch', 'scopes': ['tasks:read', 'trades:write']}]})

    def test_worker_project_list_is_limited_to_admitted_projects(self):
        projects = ['QuantStrategyLab/NewResearch', 'QuantStrategyLab/Other']
        grant = {'issuer': 'https://issuer.example', 'identity': 'worker', 'subject': 'user',
                 'projects': projects, 'provider': 'dot', 'claims': {'azp': 'worker-client'},
                 'scopes': ['tasks:read', 'tasks:work', 'events:subscribe']}
        validate_configuration({'projects': dict.fromkeys(projects, {}), 'grants': [grant]})
        with self.assertRaises(ValueError):
            validate_configuration({'projects': {projects[0]: {}}, 'grants': [grant]})

    def test_worker_list_cannot_admit_other_owners_or_submitters(self):
        project = 'QuantStrategyLab/NewResearch'
        grant = {'issuer': 'https://issuer.example', 'projects': [project], 'provider': 'grok',
                 'claims': {'azp': 'worker-client'}, 'scopes': ['tasks:work']}
        for changes in [{'projects': [project, 'OtherOrg/Repo']}, {'projects': ['QuantStrategyLab/*']},
                        {'scopes': ['tasks:work', 'tasks:submit']}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_configuration({'projects': {project: {}}, 'grants': [{**grant, **changes}]})


if __name__ == '__main__':
    unittest.main()

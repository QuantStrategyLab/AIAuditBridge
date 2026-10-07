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


if __name__ == '__main__':
    unittest.main()

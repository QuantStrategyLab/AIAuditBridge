"""Offline contracts: safe defaults, bounded routes and legacy compatibility."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

from ai_service.scenarios import (
    ContractError, compile_task_spec, load_instance, load_service_config,
    operation_key, schema, validate_task_spec,
)


FIELDS = {
    'backtest': ['study_ref', 'data_manifest_ref', 'parameters_ref', 'windows_ref', 'cost_model_ref', 'initial_state_ref'],
    'forward_observation': ['registration_ref', 'calendar_ref', 'sessions_ref', 'initial_state_ref', 'ledger_ref', 'finality_policy_ref'],
    'compliance_watch': ['targets_ref', 'coverage_ref', 'sources_ref', 'rules_ref', 'window_ref'],
    'report_audit': ['materials_ref', 'checklist_ref'],
    'bounded_transform': ['source_ref', 'operation', 'invariants_ref'],
    'investigation': ['symptoms_ref', 'code_revision', 'read_allowlist_ref', 'checklist_ref'],
    'content_pipeline': ['media_manifest_ref', 'history_ref', 'privacy_policy_ref', 'language'],
    'protocol_probe': ['fixture_ref', 'assertions_ref', 'cleanup_ref', 'ttl_seconds'],
}
PHASE = dict(zip(FIELDS, ['request', 'request', 'review', 'review', 'run', 'analyze', 'select', 'agent']))
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def instance(template='report_audit', worker='dot'):
    fields = {name: 'fixture-v1' for name in FIELDS[template]}
    if template == 'bounded_transform':
        fields['operation'] = 'translate'
    if template == 'protocol_probe':
        fields['ttl_seconds'] = 900
    return {
        'schema_version': 'scenario.instance.v1', 'id': 'example-review',
        'project': 'example-org/example-project', 'template': template, 'template_version': '1',
        'config_revision': 'a' * 40,
        'inputs': {'manifest_ref': 'frozen-v1', 'data_classification': 'synthetic', 'fields': fields},
        'stages': [{'phase': 'api' if template == 'protocol_probe' and worker == 'api' else PHASE[template],
                    'role': 'primary', 'worker_type': worker, 'route_ref': 'default', 'model_ref': 'configured'}],
        'executor_ref': 'cloud-executor-v1' if template in {'backtest', 'forward_observation'} else None,
        'output': {name: 'existing-v1' for name in ['schema_ref', 'evidence_store_ref', 'public_projection_ref', 'retention_policy_ref', 'sink_ref']},
    }


def active(template='report_audit', worker='dot'):
    value = instance(template, worker)
    value['enabled'] = True
    value['approvals'] = {'activation_ref': 'existing-approval-v1'}
    if worker == 'api':
        value['approvals']['cost_ref'] = 'existing-cost-approval-v1'
        value['stages'][0]['limits'] = {'monthly_budget_usd': 1}
        if template == 'report_audit':
            value['inputs']['fields']['small_review'] = True
    return value


def compile_example(value=None, **kw):
    value = active() if value is None else value
    return compile_task_spec(value, occurrence='2026-01', phase=value['stages'][0]['phase'],
                             role='primary', input_manifest_sha256='b' * 64, submitted_at=NOW, **kw)


class ScenarioContractTests(unittest.TestCase):
    def test_all_eight_templates_normalize_and_compile_without_mutation(self):
        for template in FIELDS:
            with self.subTest(template=template):
                source = instance(template)
                before = copy.deepcopy(source)
                normalized = load_instance(source)
                self.assertIs(normalized['enabled'], False)
                self.assertEqual(normalized['schedule']['kind'], 'manual')
                self.assertEqual(normalized['stages'][0]['limits']['monthly_budget_usd'], 0)
                self.assertEqual(source, before)
                task = compile_example(active(template))
                validate_task_spec(task, instance=active(template))
                self.assertEqual(task['template'], template)
                self.assertEqual(task['deadline'], '2026-01-01T00:10:00Z')

    def test_default_worker_is_dot_and_explicit_values_are_preserved(self):
        value = instance()
        del value['stages'][0]['worker_type']
        self.assertEqual(load_instance(value)['stages'][0]['worker_type'], 'dot')
        value['stages'][0]['worker_type'] = 'grok'
        value['stages'][0]['limits'] = {'monthly_budget_usd': 2, 'timeout_seconds': 300}
        loaded = load_instance(value)
        self.assertEqual(loaded['stages'][0]['worker_type'], 'grok')
        self.assertEqual(loaded['stages'][0]['limits']['monthly_budget_usd'], 2)

    def test_long_templates_reject_api_in_both_published_schemas(self):
        for template in set(FIELDS) - {'report_audit', 'bounded_transform', 'protocol_probe'}:
            with self.subTest(template=template):
                with self.assertRaises(ContractError):
                    load_instance(instance(template, 'api'))
                task = compile_example(active(template))
                task['worker_type'] = 'api'
                with self.assertRaises(ContractError):
                    validate_task_spec(task)

    def test_api_requires_explicit_small_review_and_bounded_limits(self):
        with self.assertRaises(ContractError):
            load_instance(instance(worker='api'))
        for template in ['report_audit', 'bounded_transform', 'protocol_probe']:
            value = active(template, 'api')
            task = compile_example(value)
            self.assertEqual(task['limits']['timeout_seconds'], 60)
            self.assertEqual(task['limits']['max_calls'], 1)
            for field, invalid in [('max_calls', 2), ('timeout_seconds', 61), ('max_input_tokens', 16001),
                                   ('max_output_tokens', 2501), ('retry_count', 1), ('max_active', 2)]:
                changed = copy.deepcopy(value)
                changed['stages'][0]['limits'][field] = invalid
                with self.subTest(template=template, field=field), self.assertRaises(ContractError):
                    load_instance(changed)
            changed = copy.deepcopy(value)
            changed['stages'][0]['tool_sets'] = ['task-worker']
            with self.assertRaises(ContractError):
                load_instance(changed)

    def test_api_zero_budget_can_be_loaded_but_cannot_compile(self):
        value = active('bounded_transform', 'api')
        value['stages'][0]['limits']['monthly_budget_usd'] = 0
        load_instance(value)
        with self.assertRaises(ContractError):
            compile_example(value)
        task = compile_example(active('bounded_transform', 'api'))
        task['limits']['monthly_budget_usd'] = 0
        with self.assertRaises(ContractError):
            validate_task_spec(task)

    def test_disabled_or_unapproved_activation_cannot_compile(self):
        with self.assertRaises(ContractError):
            compile_example(instance())
        value = active()
        value['approvals'] = {}
        with self.assertRaises(ContractError):
            load_instance(value)
        value = active('bounded_transform', 'api')
        value['approvals'].pop('cost_ref')
        with self.assertRaises(ContractError):
            load_instance(value)
        value = active()
        value['inputs']['data_classification'] = 'restricted'
        with self.assertRaises(ContractError):
            load_instance(value)

    def test_fixed_fields_versions_phases_and_no_workflow_dsl(self):
        mutations = [lambda v: v.update(template='interactive_session'),
                     lambda v: v.update(template_version='2'),
                     lambda v: v.update(config_revision='main'),
                     lambda v: v.update(project='*/project'),
                     lambda v: v.update(workflow={'shell': 'anything'}),
                     lambda v: v['inputs']['fields'].update(shell='anything'),
                     lambda v: v['inputs']['fields'].pop('materials_ref'),
                     lambda v: v['stages'][0].update(phase='execute'),
                     lambda v: v['stages'][0].update(next='arbitrary'),
                     lambda v: v['stages'][0].update(route_ref='https://untrusted.example'),
                     lambda v: v['stages'][0].update(limits={'monthly_budget_usd': float('nan')}),
                     lambda v: v.update(stages=v['stages'] * 2)]
        for mutate in mutations:
            value = instance()
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ContractError):
                load_instance(value)

    def test_schedules_require_one_matching_trigger_and_valid_timezone(self):
        for kind, trigger in [('cron', 'cron'), ('calendar', 'calendar_ref'), ('event', 'event_ref')]:
            value = instance()
            value['schedule'] = {'kind': kind}
            with self.assertRaises(ContractError):
                load_instance(value)
            value['schedule'][trigger] = 'daily-v1' if kind != 'cron' else '0 0 * * *'
            load_instance(value)
            value['schedule']['event_ref' if kind != 'event' else 'cron'] = 'wrong-v1'
            with self.assertRaises(ContractError):
                load_instance(value)
        for schedule in [{'kind': 'manual', 'cron': '0 0 * * *'}, {'timezone': 'Not/AZone'}]:
            with self.assertRaises(ContractError):
                load_instance({**instance(), 'schedule': schedule})

    def test_probe_rejects_private_materials_wrong_transport_and_unbounded_cleanup(self):
        for changes in [{'data_classification': 'restricted'}, {'fields': {'ttl_seconds': 901}}]:
            value = instance('protocol_probe')
            if 'fields' in changes:
                value['inputs']['fields'].update(changes['fields'])
            else:
                value['inputs'].update(changes)
            with self.assertRaises(ContractError):
                load_instance(value)
        value = instance('protocol_probe')
        value['stages'][0]['phase'] = 'api'
        with self.assertRaises(ContractError):
            load_instance(value)
        value = instance('backtest')
        value['executor_ref'] = None
        with self.assertRaises(ContractError):
            load_instance(value)

    def test_operation_identity_is_stable_and_binds_frozen_revision(self):
        first = compile_example()
        second = compile_example()
        self.assertEqual(first, second)
        self.assertEqual(first['operation_key'], operation_key(first))
        value = active()
        value['config_revision'] = 'c' * 40
        self.assertNotEqual(first['operation_key'], compile_example(value)['operation_key'])
        changed = copy.deepcopy(first)
        changed['input_manifest_sha256'] = 'd' * 64
        self.assertEqual(first['operation_key'], operation_key(changed))
        for name in ['project', 'instance_id', 'config_revision', 'occurrence', 'phase', 'role']:
            changed = copy.deepcopy(first)
            changed[name] += 'x'
            self.assertNotEqual(first['operation_key'], operation_key(changed))

    def test_task_validation_checks_binding_not_only_model_echo(self):
        task = compile_example()
        for field, value in [('route_ref', 'different'), ('model_ref', 'different'),
                             ('output_schema_ref', 'different'), ('inputs', {**task['inputs'], 'manifest_ref': 'different'})]:
            changed = copy.deepcopy(task)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_task_spec(changed, instance=active())
        for field, value in [('operation_key', 'scn:' + '0' * 64), ('deadline', '2026-01-01'),
                             ('deadline', 'not-a-date'), ('input_manifest_sha256', 'not-a-hash')]:
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_task_spec({**task, field: value})
        with self.assertRaises(ContractError):
            compile_task_spec(active(), occurrence='2026-01', phase='review', role='primary',
                              input_manifest_sha256='b' * 64, submitted_at=datetime(2026, 1, 1))

    def test_schemas_are_packaged_and_independently_usable(self):
        for kind in ['instance', 'task']:
            document = schema(kind)
            Draft202012Validator.check_schema(document)
            self.assertEqual(document['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        loaded = load_instance(instance())
        loaded['stages'][0]['worker_type'] = 'api'
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema('instance')).validate(loaded)

    def test_legacy_service_json_is_copied_without_normalizing_grants(self):
        # Opaque legacy values must reach the existing service validator unchanged.
        legacy = {'enabled': True, 'owner': 'example-org', 'grants': [{'project': 'example-org/project', 'scopes': ['tasks:read']}],
                  'providers': {'dot': {'type': 'dot_events'}}, 'legacy_extension': {'keep': True}}
        before = copy.deepcopy(legacy)
        with patch('socket.create_connection', side_effect=AssertionError('network access')):
            loaded = load_service_config(legacy)
        self.assertEqual(loaded.service, before)
        self.assertEqual(loaded.instances, ())
        loaded.service['grants'][0]['scopes'].append('tasks:work')
        self.assertEqual(legacy, before)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'legacy.json'
            path.write_text(json.dumps(legacy))
            self.assertEqual(load_service_config(path).service, legacy)

    def test_versioned_service_envelope_is_opt_in_and_never_activates_legacy_routes(self):
        legacy = {'enabled': False, 'grants': []}
        envelope = {'schema_version': 'scenario.service.v1', 'service': legacy, 'instances': [instance()]}
        result = load_service_config(envelope)
        self.assertEqual(result.service, legacy)
        self.assertFalse(result.instances[0]['enabled'])
        for changed in [{**envelope, 'schema_version': 'unknown'}, {**envelope, 'unknown': True},
                        {**envelope, 'instances': [instance(), instance()]}, {**envelope, 'instances': {}},
                        {**envelope, 'service': []}]:
            with self.assertRaises(ContractError):
                load_service_config(changed)

    def test_errors_do_not_echo_untrusted_values(self):
        with self.assertRaises(ContractError) as caught:
            load_instance({**instance(), 'private-value-never-echo': 'sensitive-value'})
        self.assertNotIn('sensitive-value', str(caught.exception))
        self.assertNotIn('private-value', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

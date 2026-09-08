"""Offline regression of explicit subscription routing and frozen provider identity."""
import json
from unittest.mock import Mock, patch

import pytest

from client.config import GatewayConfig
from client.gateway_client import AiGatewayClient
from service import ai_gateway_service as gateway
from service.contracts import parse_execute_request


class Reply:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return json.dumps(self.payload).encode()


@pytest.mark.parametrize('providers', [[], ['gpt'], ['cursor', 'codex'], ['codex', 'codex'], 'cursor'])
def test_reject_invalid_execution_provider_request(providers):
    with pytest.raises(ValueError):
        parse_execute_request({'prompt': 'synthetic', 'allowed_providers': providers})


def test_client_accepts_cursor_only_when_opted_in_and_identity_is_frozen():
    route = {'provider': 'cursor', 'research_stage': 'drift_analysis', 'model': 'synthetic-model', 'reasoning_effort': 'medium'}
    for mutation in ({}, {'provider': 'codex'}, {'model': 'other'}, {'reasoning_effort': 'high'}):
        replies = [Reply({'subscription_research_routing': 'v1'}), Reply({'job_id': 'test', **route}),
                   Reply({'job_id': 'test', 'status': 'succeeded', 'output': 'advisory', **route, **mutation})]
        client = AiGatewayClient(GatewayConfig(service_url='https://synthetic.invalid'))
        with patch('client.gateway_client._fetch_oidc_token', return_value='synthetic'), patch(
            'client.gateway_client.urllib.request.urlopen', side_effect=replies
        ) as http, patch('client.gateway_client.time.sleep'):
            result = client.execute('synthetic', research_stage='drift_analysis', allowed_providers=['cursor'])
        assert result.success is (not mutation)
        if not mutation:
            assert result.provider == 'cursor'
            assert result.output == 'advisory'
        assert sum(call.args[0].get_method() == 'POST' for call in http.call_args_list) == 1


def test_legacy_sdk_request_rejects_explicit_cursor_response():
    replies = [Reply({'codex_research_routing': 'v1'}), Reply({'job_id': 'test'}), Reply({
        'status': 'succeeded', 'provider': 'cursor', 'model': 'synthetic-model',
        'research_stage': 'drift_analysis', 'reasoning_effort': 'medium', 'output': 'advisory'})]
    client = AiGatewayClient(GatewayConfig(service_url='https://synthetic.invalid'))
    with patch('client.gateway_client._fetch_oidc_token', return_value='synthetic'), patch(
        'client.gateway_client.urllib.request.urlopen', side_effect=replies
    ), patch('client.gateway_client.time.sleep'):
        result = client.execute('synthetic', research_stage='drift_analysis')
    assert not result.success
    assert not result.output


def test_provider_is_part_of_public_job_and_dedupe():
    job = {'provider': 'cursor', 'research_stage': 'drift_analysis', 'model': 'synthetic', 'reasoning_effort': 'medium'}
    assert gateway._public_job_payload(job)['provider'] == 'cursor'
    args = {'repository': 'Synthetic/caller', 'run_id': '1', 'run_attempt': '1'}
    assert gateway._job_dedupe_key(job, **args) != gateway._job_dedupe_key({**job, 'provider': 'codex'}, **args)


def test_cursor_admission_is_not_inferred_from_request_alone():
    payload = {'prompt': 'synthetic', 'mode': 'review_only', 'research_stage': 'drift_analysis', 'allowed_providers': ['cursor']}
    with patch.dict(gateway.os.environ, {}, clear=True):
        denial = gateway._admit_codex_execute(Mock(), 'Synthetic/caller', payload)
    assert denial['status'] == 'deferred'
    assert denial['execution_started'] is False


def test_cursor_route_requires_fresh_account_and_explicit_spend_quality_policy():
    from service.cursor_account import resolve_cursor_route
    roster = {'status': 'available', 'source': 'cursor_cli_account', 'updated_at': 900,
              'models': ['synthetic-model', 'unknown-new-model']}
    policy = {'valid_until': 2000, 'on_demand_disabled_verified': True, 'max_daily_calls': 2,
              'models': {'synthetic-model': {'quality_level': 2, 'supported_reasoning_efforts': ['high']}}}
    payload = {'research_stage': 'optimization', 'mode': 'review_only'}
    result = resolve_cursor_route(payload, {}, policy=policy, roster=roster, now=1000)
    assert result == {'action': 'run', 'provider': 'cursor', 'model': 'synthetic-model', 'reasoning_effort': 'high'}
    for change in ({'status': 'stale'}, {'updated_at': 1001}, {'updated_at': -90000}, {'source': 'openai_api'}, {'models': ['unknown-new-model']}):
        assert resolve_cursor_route(payload, {}, policy=policy, roster={**roster, **change}, now=1000)['action'] == 'defer'
    for change in ({'on_demand_disabled_verified': False}, {'valid_until': 999}, {'max_daily_calls': 0}, {'models': {}}):
        assert resolve_cursor_route(payload, {}, policy={**policy, **change}, roster=roster, now=1000)['action'] == 'defer'
    assert resolve_cursor_route(payload, {'cursor_calls': 2}, policy=policy, roster=roster, now=1000)['action'] == 'defer'


def test_fallback_only_after_eligible_pre_execution_codex_deferral():
    quota = Mock()
    quota._codex_account_snapshot.return_value = None
    payload = {'prompt': 'synthetic', 'mode': 'review_only', 'research_stage': 'optimization', 'allowed_providers': ['codex', 'cursor']}
    with patch.dict(gateway.os.environ, {'AI_GATEWAY_CURSOR_FALLBACK_ENABLED': 'true'}, clear=True), patch.object(
        gateway, 'resolve_codex_research_route', return_value={'action': 'defer', 'reason': 'codex_quota_reserved', 'retry_at': 2000}
    ), patch.object(gateway, '_admit_cursor_execute', return_value=None) as cursor:
        assert gateway._admit_codex_execute(quota, 'Synthetic/caller', dict(payload)) is None
        cursor.assert_called_once()
        cursor.reset_mock()
        gateway._admit_codex_execute(quota, 'Synthetic/caller', {**payload, 'model': 'explicit-codex'})
        gateway._admit_codex_execute(quota, 'Synthetic/caller', {**payload, 'allowed_providers': ['codex']})
        cursor.assert_not_called()


def test_cursor_quota_is_counted_separately_and_cost_is_unknown():
    from service.quota import QuotaManager
    manager = QuotaManager()
    with patch.object(manager, '_save_records_locked'):
        manager.record_execute('Synthetic/caller', provider='cursor')
    status = manager.status('Synthetic/caller')
    assert status['cursor_calls'] == 1
    assert status['codex_calls'] == 0
    summary = manager._summary_from_statuses({'Synthetic/caller': status})
    assert summary['cursor']['total_cost_usd'] is None


def test_catalog_subscription_refresh_preserves_api_and_last_known_good(tmp_path):
    from service.model_catalog import ModelCatalog, allow_catalog_parent, save_catalog_atomic, load_catalog
    from service.model_catalog_sync import sync_subscription_catalog
    target = allow_catalog_parent(tmp_path) / 'model_catalog.json'
    original = ModelCatalog(catalog_source='synthetic', synced_at='2026-09-09T00:00:00Z')
    save_catalog_atomic(original, target)
    fresh = {'status': 'available', 'source': 'cursor_cli_account', 'updated_at': 1000, 'models': ['synthetic-model', 'unknown-new']}
    with patch('service.model_catalog_sync.discover_cursor_roster', return_value=fresh):
        sync_subscription_catalog(output_path=str(target), now=1000)
    first = load_catalog(target)
    assert first.synced_at == original.synced_at
    assert first.subscription_rosters['cursor'] == fresh
    with patch('service.model_catalog_sync.discover_cursor_roster', return_value=None):
        sync_subscription_catalog(output_path=str(target), now=2000)
    stale = load_catalog(target).subscription_rosters['cursor']
    assert stale['status'] == 'stale'
    assert stale['models'] == fresh['models']
    assert stale['updated_at'] == 1000
    assert stale['last_refresh_attempt_at'] == 2000


def test_portfolio_real_sdk_cursor_optin_retains_advisory_and_one_comment():
    from tests.test_portfolio_research_proposal import _readiness
    from scripts.run_portfolio_research_proposal_diagnosis import run_portfolio_research_proposal_diagnosis
    route = {'provider': 'cursor', 'model': 'synthetic-model', 'reasoning_effort': 'medium', 'research_stage': 'drift_analysis'}
    replies = [Reply({'subscription_research_routing': 'v1'}), Reply({'job_id': 'test', **route}),
               Reply({'job_id': 'test', 'status': 'succeeded', 'output': 'synthetic advisory', **route})]
    comment = Mock(return_value='https://example.invalid/comment/1')
    with patch.dict('os.environ', {'CODEX_AUDIT_SERVICE_URL': 'https://synthetic.invalid', 'AI_GATEWAY_RESEARCH_PROVIDERS': 'cursor'}, clear=True), patch(
        'client.gateway_client._fetch_oidc_token', return_value='synthetic'
    ), patch('client.gateway_client.urllib.request.urlopen', side_effect=replies) as http, patch('client.gateway_client.time.sleep'):
        result = run_portfolio_research_proposal_diagnosis(_readiness(), find_issue=lambda *_: 'https://example.invalid/issue/1', marker_present=lambda *_: False, create_comment=comment)
    assert result['diagnoses'][0]['provider'] == 'cursor'
    assert result['diagnoses'][0]['advisory_only'] is True
    comment.assert_called_once()
    assert 'advisory' in comment.call_args.args[2]
    assert json.loads(http.call_args_list[1].args[0].data)['allowed_providers'] == ['cursor']


def test_cli_discovery_parses_real_format_and_never_keeps_account_identity():
    from service.cursor_account import discover_cursor_roster, parse_cursor_models
    from types import SimpleNamespace
    roster = 'Available models\n\nauto - Auto (default)\ncursor-grok-4.6-high - Grok 4.6 High\nunknown-new - Unknown New\n\nTip: select a model\n'
    assert parse_cursor_models(roster) == ['auto', 'cursor-grok-4.6-high', 'unknown-new']
    for invalid in ('', 'models unavailable', 'Available models\n', 'Available models\ngarbage'):
        with pytest.raises(ValueError):
            parse_cursor_models(invalid)
    with patch('service.cursor_account.shutil.which', return_value='/synthetic/agent'), patch(
        'service.cursor_account.subprocess.run', side_effect=[SimpleNamespace(returncode=0, stdout='✓ Logged in as synthetic-private@example.invalid'), SimpleNamespace(returncode=0, stdout=roster)]
    ) as runner:
        snapshot = discover_cursor_roster()
    assert snapshot['status'] == 'available'
    assert 'synthetic-private' not in repr(snapshot)
    assert [call.args[0][-1] for call in runner.call_args_list] == ['status', 'models']


def test_started_codex_failure_never_starts_cursor():
    from types import SimpleNamespace
    job = {'job_id': 'synthetic', 'status': 'queued', 'task': 'execute', 'provider': 'codex',
           'model': 'synthetic-model', 'research_stage': 'drift_analysis', 'reasoning_effort': 'medium'}
    with patch.object(gateway, '_read_job', return_value=job), patch.object(gateway, '_write_job'), patch.object(
        gateway, '_record_job_automation_run'
    ), patch.object(gateway, '_audit_log'), patch.object(gateway, 'get_health_monitor'), patch.object(
        gateway, '_record_platform_execution_telemetry'
    ), patch.object(gateway, 'CodexAdapter') as codex, patch.object(gateway, 'CursorAdapter') as cursor:
        codex.return_value.execute.return_value = SimpleNamespace(success=False, error='codex exec timed out', output='')
        gateway._run_job('synthetic', {'prompt': 'synthetic', 'allowed_providers': ['codex', 'cursor'], **job})
    assert job['status'] == 'failed'
    codex.return_value.execute.assert_called_once()
    cursor.assert_not_called()


def test_cursor_usage_is_global_and_corrupt_store_cannot_reset_allowance(tmp_path):
    from service.quota import QuotaManager
    with patch.dict('os.environ', {'CODEX_AUDIT_SERVICE_QUOTA_STORE': str(tmp_path / 'quota.json')}):
        quota = QuotaManager()
        quota.record_execute('Synthetic/one', provider='cursor')
        quota.record_execute('Synthetic/two', provider='cursor')
        assert quota.cursor_usage() == {'status': 'available', 'cursor_calls': 2}
        assert QuotaManager().cursor_usage()['cursor_calls'] == 2
        quota._store_available = False
        assert quota.cursor_usage()['status'] == 'unavailable'


@pytest.mark.parametrize('failure', ['job', 'http', 'transport'])
def test_research_sdk_never_returns_failure_diagnostics(failure):
    import io
    import urllib.error
    marker = 'private-diagnostic-must-not-propagate'
    route = {'provider': 'cursor', 'model': 'synthetic-model', 'research_stage': 'drift_analysis', 'reasoning_effort': 'medium'}
    if failure == 'job':
        replies = [Reply({'subscription_research_routing': 'v1'}), Reply({'job_id': 'test', **route}), Reply({'job_id': 'test', 'status': 'failed', 'error': marker, 'private': marker, **route})]
    elif failure == 'http':
        replies = [Reply({'subscription_research_routing': 'v1'}), urllib.error.HTTPError('https://example.invalid', 500, marker, {}, io.BytesIO(marker.encode()))]
    else:
        replies = [urllib.error.URLError(marker)]
    client = AiGatewayClient(GatewayConfig(service_url='https://synthetic.invalid'))
    with patch('client.gateway_client._fetch_oidc_token', return_value='synthetic'), patch('client.gateway_client.urllib.request.urlopen', side_effect=replies), patch('client.gateway_client.time.sleep'):
        result = client.execute('synthetic', research_stage='drift_analysis', allowed_providers=['cursor'])
    assert not result.success
    assert marker not in repr(result)


def test_subscription_sdk_rejects_different_job_with_same_route():
    route = {'provider': 'cursor', 'model': 'synthetic-model', 'research_stage': 'drift_analysis', 'reasoning_effort': 'medium'}
    client = AiGatewayClient(GatewayConfig(service_url='https://synthetic.invalid'))
    with patch('client.gateway_client._fetch_oidc_token', return_value='synthetic'), patch('client.gateway_client.time.sleep'), patch(
        'client.gateway_client.urllib.request.urlopen', side_effect=[Reply({'subscription_research_routing': 'v1'}), Reply({'job_id': 'original', **route}), Reply({'job_id': 'different', 'status': 'succeeded', 'output': 'text', **route})]
    ):
        result = client.execute('synthetic', research_stage='drift_analysis', allowed_providers=['cursor'])
    assert not result.success


@pytest.mark.parametrize('first_provider', ['cursor', 'codex'])
def test_repeated_active_request_preserves_original_job_route_and_single_reservation(tmp_path, first_provider):
    from service.quota import QuotaManager
    providers = ['cursor'] if first_provider == 'cursor' else ['codex', 'cursor']
    request = {'prompt': 'synthetic', 'mode': 'review_only', 'research_stage': 'drift_analysis', 'allowed_providers': providers}
    cursor_model = 'synthetic-cursor'
    def cursor_route(payload, usage, **kwargs):
        if usage['cursor_calls'] >= 2:
            return {'action': 'defer', 'provider': 'cursor', 'reason': 'cursor_capacity_unavailable'}
        return {'action': 'run', 'provider': 'cursor', 'model': cursor_model, 'reasoning_effort': 'medium'}
    codex_routes = [{'action': 'run', 'provider': 'codex', 'model': 'synthetic-codex', 'reasoning_effort': 'medium'},
                    {'action': 'defer', 'reason': 'codex_quota_reserved', 'retry_at': 9000},
                    {'action': 'defer', 'reason': 'codex_quota_reserved', 'retry_at': 9000}]
    with patch.dict('os.environ', {'CODEX_AUDIT_SERVICE_JOB_DIR': str(tmp_path / 'jobs'),
        'CODEX_AUDIT_SERVICE_QUOTA_STORE': str(tmp_path / 'quota.json'), 'AI_GATEWAY_CURSOR_FALLBACK_ENABLED': 'true'}, clear=True):
        quota = QuotaManager()
        with patch.object(gateway, 'get_quota_manager', return_value=quota), patch.object(quota, '_codex_account_snapshot', return_value=None), patch.object(
            gateway, 'resolve_codex_research_route', side_effect=codex_routes
        ), patch('service.cursor_account.cursor_research_route', side_effect=cursor_route) as cursor, patch.object(
            gateway.threading, 'Thread'
        ) as threads, patch.object(gateway, '_record_job_automation_run'), patch.object(gateway, '_audit_log'), patch.object(
            gateway, 'get_health_monitor'
        ), patch.object(gateway, '_json_response') as response:
            for _ in range(3):
                gateway.AiGatewayRequestHandler._handle_execute_async(object(), {'repository': 'Synthetic/caller', 'run_id': '1'}, dict(request))
        bodies = [call.args[2] for call in response.call_args_list]
        assert [call.args[1] for call in response.call_args_list] == [202, 202, 202]
        assert len({body['job_id'] for body in bodies}) == 1
        assert [body['provider'] for body in bodies] == [first_provider] * 3
        assert threads.call_count == 1
        status = quota.status('Synthetic/caller')
        assert status['cursor_calls'] == (1 if first_provider == 'cursor' else 0)
        assert status['codex_calls'] == (1 if first_provider == 'codex' else 0)
        assert cursor.call_count == (1 if first_provider == 'cursor' else 0)


def test_original_request_identity_binds_route_inputs_and_ignores_claimed_provider():
    claims = {'repository': 'Synthetic/caller', 'run_id': '1'}
    payload = {'prompt': 'synthetic', 'research_stage': 'drift_analysis', 'allowed_providers': ['codex', 'cursor']}
    key = gateway._request_job_dedupe_key(claims, payload)
    for change in ({'model': 'explicit-model'}, {'reasoning_effort': 'high'}, {'research_stage': 'optimization'},
                   {'allowed_providers': ['cursor']}, {'complexity': 'high'}):
        assert gateway._request_job_dedupe_key(claims, {**payload, **change}) != key
    assert gateway._request_job_dedupe_key(claims, {**payload, 'provider': 'cursor'}) == key


def test_active_cap_rejects_new_request_before_quota_reservation(tmp_path):
    from service.quota import QuotaManager
    with patch.dict('os.environ', {'CODEX_AUDIT_SERVICE_JOB_DIR': str(tmp_path / 'jobs'), 'CODEX_AUDIT_SERVICE_MAX_ACTIVE_JOBS': '1'}, clear=True):
        quota = QuotaManager()
        with patch.object(gateway, 'get_quota_manager', return_value=quota), patch.object(gateway, '_active_job_count', return_value=1), patch.object(
            quota, 'record_execute'
        ) as record, patch.object(gateway, '_admit_codex_execute') as admission:
            with pytest.raises(PermissionError):
                gateway.AiGatewayRequestHandler._handle_execute_async(object(), {'repository': 'Synthetic/caller'}, {'prompt': 'new request'})
        admission.assert_not_called()
        record.assert_not_called()


@pytest.mark.parametrize('requested', ['', 'auto'])
def test_research_stage_defaults_ignore_legacy_global_model_and_effort(requested):
    from service.quota import QuotaManager
    account = {'status': 'available', 'updated_at': 1000,
        'rate_limits': {'primary': {'used_percent': 10, 'window_duration_mins': 10080, 'resets_at': 9000}},
        'available_models': [{'model': 'gpt-5.6-terra', 'supported_reasoning_efforts': ['medium']}]}
    quota = QuotaManager()
    payload = {'prompt': 'synthetic', 'research_stage': 'drift_analysis', 'model': requested, 'reasoning_effort': requested}
    with patch.dict('os.environ', {'CODEX_AUDIT_SERVICE_MODEL': 'gpt-5.4', 'CODEX_AUDIT_SERVICE_REASONING_EFFORT': 'low'}, clear=True), patch.object(
        quota, '_codex_account_snapshot', return_value=account
    ), patch.object(gateway.time, 'time', return_value=1000):
        denial = gateway._admit_codex_execute(quota, 'Synthetic/caller', payload)
    assert denial is None
    assert payload['model'] == 'gpt-5.6-terra'
    assert payload['reasoning_effort'] == 'medium'


def test_explicit_research_model_is_never_replaced_by_stage_default():
    from service.quota import QuotaManager
    payload = {'prompt': 'synthetic', 'research_stage': 'drift_analysis', 'model': 'gpt-5.4'}
    quota = QuotaManager()
    with patch.object(quota, '_codex_account_snapshot', return_value=None):
        denial = gateway._admit_codex_execute(quota, 'Synthetic/caller', payload)
    assert denial['error'] == 'codex_model_quota_mapping_unavailable'
    assert payload['model'] == 'gpt-5.4'

import copy
import json
import subprocess

import pytest

from scripts import run_soxl_manual_learning as consumer


VARIANTS = ('baseline_mid_065', 'soxx_buy_hold', 'fixed_full_weights')


def numeric_result():
    results = []
    for variant in VARIANTS:
        for cost in consumer.COST_BPS:
            results.append({
                'variant': variant, 'cost_bps': cost,
                'initial_equity': 100_000.0, 'final_equity': 100_900.0,
                'total_return': .009, 'max_drawdown': .001,
                'cost_total': 100.0, 'one_way_turnover': 2.0,
                'asset_pnl_usd': {'SOXL': 600.0, 'SOXX': 300.0, 'BOXX': 100.0},
                'cash_pnl_usd': 0.0, 'external_flow_usd': 0.0,
                'reconciliation_residual_usd': 0.0,
                'contribution_pct_points': {'SOXL': .6, 'SOXX': .3, 'BOXX': .1, 'cash': 0.0, 'execution_cost': -.1},
                'start_date': '2023-01-03', 'end_date': '2025-07-31',
                'observation_count': 600, 'unexecuted_final_signal': True,
            })
    value = {
        'schema_version': 'qsl.soxl-three-asset-attribution.v1',
        'status': 'SUCCESS', 'study_kind': 'retrospective_research',
        'learning_only': True, 'research_executed': True, 'no_order': True,
        'size_zero_required': True, 'promotion_eligible': False,
        'development_cutoff': consumer.DEVELOPMENT_CUTOFF,
        'p1_identity': {'input_manifest_sha256': 'a' * 64},
        'source_identity': {'repository': 'QuantStrategyLab/UsEquityStrategies',
                            'revision': consumer.UES_REVISION,
                            'quant_platform_kit_revision': consumer.SOXL_WATCHER_QPK_REVISION,
                            'uv_lock_sha256': '6c12df9b3412681829295f15de7e2ce7fc5b708d1de815f72d654fc16b7848e6'},
        'results': results,
    }
    value['result_sha256'] = consumer._summary_digest(value)
    return value


def test_attribution_accepts_only_fixed_aggregate_results():
    result = numeric_result()
    safe = consumer._sanitize_attribution(result, 'a' * 64)
    assert len(safe['results']) == 9
    assert safe['study_kind'] == 'retrospective_research'
    assert safe['promotion_eligible'] is False
    assert 'p1_identity' not in safe


@pytest.mark.parametrize('mutation', [
    lambda x: x['results'][0].update(final_equity=101_000),
    lambda x: x['results'][0].update(total_return=float('nan')),
    lambda x: x['results'][0].update(variant='optimized_winner'),
    lambda x: x['results'][0].update(end_date='2026-08-04'),
    lambda x: x['results'][0].update(contribution_pct_points={'SOXL': .9}),
    lambda x: x['results'].pop(),
    lambda x: x.update(promotion_eligible=True),
    lambda x: x.update(result_sha256='0' * 64),
])
def test_attribution_rejects_bad_identity_math_or_trials(mutation):
    result = numeric_result()
    mutation(result)
    if result.get('result_sha256') != '0' * 64:
        result.pop('result_sha256')
        try:
            result['result_sha256'] = consumer._summary_digest(result)
        except ValueError:
            result['result_sha256'] = 'invalid'
    with pytest.raises(consumer.ManualLearningError):
        consumer._sanitize_attribution(result, 'a' * 64)


def test_unexpected_numeric_series_never_cross_artifact_boundary():
    result = numeric_result()
    result['results'][0]['prices'] = [{'private_price': 123}]
    result.pop('result_sha256')
    result['result_sha256'] = consumer._summary_digest(result)
    safe = consumer._sanitize_attribution(result, 'a' * 64)
    assert 'private_price' not in json.dumps(safe)
    assert 'prices' not in safe['results'][0]


def test_attribution_uses_no_model_and_does_not_retry_unknown_outcome(tmp_path):
    root, source, ues = (tmp_path / name for name in ('input', 'consumer', 'ues'))
    for path in (root / 'binding.json', root / 'manifest.json', root / 'bars.json',
                 source / 'scripts/run_soxl_three_asset_learning.py',
                 source / 'config/soxl_soxx_core_only_p2_v3.json', source / '.venv/bin/python'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
    ues.mkdir()
    calls = []
    progress = []

    def execute(command):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 1200)

    result = consumer.run_manual_attribution(
        manifest_sha256='a' * 64, root=root, consumer_source=source, ues_source=ues,
        context={'repository': consumer.EXPECTED_REPOSITORY, 'ref': consumer.EXPECTED_REF,
                 'event_name': consumer.EXPECTED_EVENT, 'actor': 'tester', 'run_id': '123', 'run_attempt': '1'},
        command_runner=execute, progress_writer=lambda x: progress.append(copy.deepcopy(x)),
    )
    assert len(calls) == 1
    assert '--attribution' in calls[0]
    assert '--blend-gate-mid-soxl-weight' not in calls[0]
    assert result['status'] == 'parked'
    assert result['failure_stage'] == 'numeric_outcome_unknown'
    assert result['research_executed'] is None
    assert progress[0]['numeric_execution']['status'] == 'started'


def test_attribution_workflow_is_explicit_and_never_publishes_as_validation():
    from pathlib import Path
    text = (Path(__file__).parents[1] / '.github/workflows/research_input_readback.yml').read_text()
    assert '- soxl_attribution' in text
    assert '--attribution' in text
    assert 'default: readback' in text
    publish = text.split('  publish-validation-result:', 1)[1]
    assert "inputs.operation == 'soxl_attribution'" not in publish


def volatility_result():
    value = numeric_result()
    value['results'] = [copy.deepcopy(value['results'][1]), copy.deepcopy(value['results'][1])]
    value['results'][1]['variant'] = 'baseline_without_volatility_delever'
    value.update(study_variant='volatility_delever_on_off_v1',
                 variants=['baseline_mid_065', 'baseline_without_volatility_delever'],
                 cost_bps=[10.0], causal_attribution_claimed=False)
    value.pop('result_sha256')
    value['result_sha256'] = consumer._summary_digest(value)
    return value


def test_volatility_ablation_accepts_only_its_fixed_pair():
    safe = consumer._sanitize_attribution(volatility_result(), 'a' * 64, volatility_ablation=True)
    assert [x['variant'] for x in safe['results']] == ['baseline_mid_065', 'baseline_without_volatility_delever']
    assert safe['study_variant'] == 'volatility_delever_on_off_v1'
    assert safe['causal_attribution_claimed'] is False
    with pytest.raises(consumer.ManualLearningError):
        consumer._sanitize_attribution(volatility_result(), 'a' * 64)


@pytest.mark.parametrize('mutation', [
    lambda x: x.update(study_variant='optimized_study'),
    lambda x: x.update(causal_attribution_claimed=True),
    lambda x: x.update(cost_bps=[5.0]),
    lambda x: x.update(variants=['baseline_mid_065', 'fixed_full_weights']),
    lambda x: x['results'][1].update(cost_bps=5.0),
    lambda x: x['results'][1].update(variant='baseline_mid_065'),
    lambda x: x['results'][1].update(observation_count=599),
])
def test_volatility_ablation_rejects_changed_trial_or_window(mutation):
    value = volatility_result()
    mutation(value)
    value.pop('result_sha256')
    value['result_sha256'] = consumer._summary_digest(value)
    with pytest.raises(consumer.ManualLearningError):
        consumer._sanitize_attribution(value, 'a' * 64, volatility_ablation=True)


def test_ablation_cli_rejects_grid_and_other_modes_before_execution():
    for other in (['--attribution'], ['--parameter-grid', '0.65'], ['--watcher-preflight']):
        with pytest.raises(SystemExit) as exc:
            consumer.main(['--volatility-ablation', *other])
        assert exc.value.code == 2


def test_ablation_unpublished_source_cannot_execute(tmp_path, monkeypatch):
    monkeypatch.setattr(consumer, 'VOLATILITY_ABLATION_CONSUMER_REVISION', '')
    calls = []
    with pytest.raises(consumer.ManualLearningError, match='volatility_ablation_source_unpinned'):
        consumer.run_manual_attribution(
            manifest_sha256='a' * 64, root=tmp_path, consumer_source=tmp_path, ues_source=tmp_path,
            context={'repository': consumer.EXPECTED_REPOSITORY, 'ref': consumer.EXPECTED_REF,
                     'event_name': consumer.EXPECTED_EVENT, 'actor': 'tester', 'run_id': '123', 'run_attempt': '1'},
            command_runner=lambda argv: calls.append(argv), volatility_ablation=True,
        )
    assert calls == []


@pytest.mark.parametrize('timeout', [False, True])
def test_ablation_executes_once_and_preserves_research_boundary(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(consumer, 'VOLATILITY_ABLATION_CONSUMER_REVISION', 'b' * 40)
    root, source, ues = (tmp_path / name for name in ('input', 'consumer', 'ues'))
    for path in (root / 'binding.json', root / 'manifest.json', root / 'bars.json',
                 source / 'scripts/run_soxl_three_asset_learning.py',
                 source / 'config/soxl_soxx_core_only_p2_v3.json', source / '.venv/bin/python'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
    ues.mkdir()
    calls = []

    def execute(command):
        calls.append(command)
        if timeout:
            raise subprocess.TimeoutExpired(command, 1200)
        return subprocess.CompletedProcess(command, 0, json.dumps(volatility_result()), '')

    result = consumer.run_manual_attribution(
        manifest_sha256='a' * 64, root=root, consumer_source=source, ues_source=ues,
        context={'repository': consumer.EXPECTED_REPOSITORY, 'ref': consumer.EXPECTED_REF,
                 'event_name': consumer.EXPECTED_EVENT, 'actor': 'tester', 'run_id': '123', 'run_attempt': '1'},
        command_runner=execute, volatility_ablation=True,
    )
    assert len(calls) == 1
    assert '--volatility-ablation' in calls[0] and '--attribution' not in calls[0]
    assert '--blend-gate-mid-soxl-weight' not in calls[0]
    assert result['operation'] == 'soxl_volatility_ablation'
    assert result['consumer_source']['revision'] == 'b' * 40
    assert result['causal_attribution_claimed'] is False
    assert result['promotion_eligible'] is False and result['no_order'] is True
    assert result['status'] == ('parked' if timeout else 'accepted')
    assert result['research_executed'] is (None if timeout else True)


def test_ablation_workflow_uses_fixed_source_and_cannot_publish_validation():
    from pathlib import Path
    text = (Path(__file__).parents[1] / '.github/workflows/research_input_readback.yml').read_text()
    assert '- soxl_volatility_ablation' in text
    assert 'VOLATILITY_ABLATION_CONSUMER_REVISION' in text
    assert '--volatility-ablation' in text
    assert 'steps.ablation-source.outputs.revision' in text
    publish = text.split('  publish-validation-result:', 1)[1]
    assert "inputs.operation == 'soxl_volatility_ablation'" not in publish

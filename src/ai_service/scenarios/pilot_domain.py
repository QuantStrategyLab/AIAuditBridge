"""Synthetic protocol fixture; no market data, strategy executor or orders."""
from . import ContractError
from .pilot import object_schema

DATA_CLASSIFICATIONS = {'synthetic'}
TEMPLATE = 'protocol_probe'
OBJECTIVE = ('Protocol probe only. Read the frozen synthetic input, claim the task once, '
             'return the exact output_schema object via report_task_result using the current '
             'attempt_id/revision. Calculate the sum of adjacent differences in series. '
             'Do not access markets, brokers, files or other tools. No orders or adoption.')


def result_contract(manifest):
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'series', 'no_order'}
            or manifest['schema_version'] != 'synthetic.protocol-input.v1'
            or manifest['no_order'] is not True or not isinstance(manifest['series'], list)
            or not 2 <= len(manifest['series']) <= 16
            or any(type(n) is not int or abs(n) > 1000 for n in manifest['series'])):
        raise ContractError('invalid synthetic protocol fixture')
    series = manifest['series']
    return object_schema({
        'synthetic': {'const': True}, 'no_order': {'const': True}, 'adoption_allowed': {'const': False},
        'sample_count': {'const': len(series)}, 'difference_sum': {'const': series[-1] - series[0]},
    })

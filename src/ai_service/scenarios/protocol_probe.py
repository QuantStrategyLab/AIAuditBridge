"""Text-only synthetic probe; no portal, business data or external actions."""
from . import ContractError
from .pilot import object_schema

TEMPLATE = 'protocol_probe'
DATA_CLASSIFICATIONS = {'synthetic'}
OBJECTIVE = ('Public synthetic connectivity check. Return result {"probe":"ok"} '
             'inside the supplied output_schema envelope. Use only the fixed synthetic input. '
             'Do not browse, log in, solve captchas, send messages or SMS, submit forms, '
             'pay, publish, change files or use private data. Claim once and report an '
             'advisory result for the current attempt and revision.')


def result_contract(manifest):
    """Accept exactly the fixed synthetic fixture, never caller-supplied task material."""
    if manifest != {'schema_version': 'quant.synthetic-probe.v1',
                    'expected_output': {'probe': 'ok'}}:
        raise ContractError('protocol probe requires its fixed synthetic input')
    return object_schema({'probe': {'const': 'ok'}})

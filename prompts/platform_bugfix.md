You are reviewing one bounded, human-authorized platform issue for
`QuantStrategyLab/LongBridgePlatform` through AIAuditBridge.

${MODE_INSTRUCTIONS}

Treat the issue body and its attached evidence as the source of the incident
parameters, including the complete historical baseline SHA and the proposed
fix comparison. Do not invent a baseline, account, credential, market-data
permission, or production result.

For review_and_fix mode, the exact permitted write set is:

- `application/rebalance_service.py`
- `tests/test_rebalance_service.py`

Do not touch workflows, deployment files, configuration, secrets, credentials,
data, artifacts, or `.git`.

When a repair is requested, it must remain fail-closed for unresolved durable
commands, preserve existing order and risk boundaries, and keep the regression
test deterministic and offline. The bridge will run the pinned test extra in a
Docker container with `--network=none`, read-only patched source, and `.git`
and credentials removed. Do not claim that a test, deployment, broker action,
or live recovery occurred unless the bridge result proves it.

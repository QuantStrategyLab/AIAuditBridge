You are executing one bounded, human-authorized platform bugfix for
`QuantStrategyLab/LongBridgePlatform` through AIAuditBridge.

Treat the issue body and its attached evidence as the source of the incident
parameters, including the complete historical baseline SHA and the proposed
fix comparison. Do not invent a baseline, account, credential, market-data
permission, or production result.

The exact permitted write set is:

- `application/rebalance_service.py`
- `tests/test_rebalance_service.py`

Return one JSON object matching the service patch contract. Return complete
file contents for exactly those two paths. Do not touch workflows, deployment
files, configuration, secrets, credentials, data, artifacts, or `.git`.

The repair must remain fail-closed for unresolved durable commands, preserve
existing order and risk boundaries, and keep the regression test deterministic
and offline. The bridge will run the pinned test extra in a Docker container
with `--network=none`, read-only patched source, and `.git` and credentials
removed. Do not claim that a test, deployment, broker action, or live recovery
occurred unless the bridge result proves it.

# Quant 2.0.13 release bundle

This release brings the independent Quant runtime to the Personal 2.0.13 class
of task, event and scenario-instance controls. The comparison reference is
PersonalAIService `cabcc30f8440162f97b83d93fe8794112505fcc5` (2.0.13).
It does not install or depend on the Personal distribution.

| Capability | Quant 2.0.13 |
| --- | --- |
| Exact owner-wide workers | Already supported; cross-owner access and worker submit scopes remain rejected. |
| Project admission | Each repository still needs its own policy and a separately approved submitter. Owner grants do not discover repositories or create policies. |
| Task/event runtime | Existing authentication, signed callbacks, attempt/revision checks, exact-project subscriptions and subscription management retained. |
| Scenario instance control | `ai-scenario-instance` adds audited readback/enable/disable with exact project, instance, approval reference and expected digest. Private external state only; no service authorization edits. |
| Fixed synthetic probe | `ai_service.scenarios.protocol_probe` accepts only `quant.synthetic-probe.v1` with `expected_output={"probe":"ok"}`. Existing `synthetic.protocol-input.v1` series fixtures retain their previous contract. |
| Worker tools preflight | Read-only `quant-ai-service --check-worker-tools`; the schema builder is shared with actual `tools/list`. Adapted from Personal PR #37 at `730a4e269837a993d137bf318304de1780bb947c`, which was not part of released Personal 2.0.13. Request logging changes from that PR are not included. |
| Deployment helper | Existing #354 Quant-only helper and gates retained; no Personal helper, Secret synchronization or automatic deployment. |

Personal's Ctrip compliance adapter is business-specific and is not a Quant
release requirement. Quant keeps its synthetic-only adapters; neither this
release nor the instance CLI introduces a trading or strategy executor.
The adapted modules retain their MIT notice in `ai_service/LICENSE`.

## Offline checks

For an already reviewed private candidate (never a live config edit):

```sh
quant-ai-service --config /private/candidate.json --check-worker-tools \
  --worker-identity example-worker \
  --expect-project example-org/Alpha --expect-project example-org/Beta
```

This requires an exact owner-wide read/work/events worker and verifies the five
tool schemas and the complete expected policy enum. It creates no DB, provider,
subscription, socket or token. A successful summary is configuration-derived;
it does not verify OAuth, the running process or the ChatGPT App.
Legacy single-project configs can still pass `--check-config` and run normally;
they cannot pass this owner-wide preflight until a separate grant change is approved.

`examples/pilots/protocol-probe/instance.json` remains disabled, has an unreviewed
revision and carries no activation approval. Offline preview only:

```sh
python -m ai_service.scenarios.pilot_cli dry-run \
  --instance examples/pilots/protocol-probe/instance.json \
  --input examples/pilots/protocol-probe/input.json \
  --occurrence example-preview --submitted-at 2026-10-16T00:00:00Z
```

The instance CLI accepts only an independently approved instance outside any
repository or fixture tree, in a private mode-700 state directory with mode-600
files. Enable/disable require the current SHA256; audit intent and readback are
durable. Matching an approval reference does not verify the external approval.
Installing the command grants no permission to activate an instance.

## Artifact and deployment boundary

CI uploads the independent wheel and the complete Python 3.12 / Linux x86_64
`quant-vps-bundle`. It checks the installed runtime modules, both schemas, license
and all three console scripts in the normal install and again after hash-pinned,
offline installation of the exact bundle. Tests include both synthetic probe
formats, a signed local lifecycle, instance audit failures and worker schema parity.

PR artifacts are review artifacts. A deployable artifact must come from a
successful default-branch **push** of `.github/workflows/ci.yml`, at the exact
approved full source SHA. Record run ID/attempt, artifact ID/expiration,
`release.json`, wheel SHA256, outer bundle SHA256 and `SHA256SUMS`; independently
run the existing `ci_release.py verify`. The wheel/bundle are CI artifacts, not a
GitHub Release or a PyPI publication. They expire; never substitute a rebuild or
PR artifact under an existing approval.

Future workflow inputs are `action=deploy`, `ref=FULL_SOURCE_SHA`,
`artifact_run_id=EXACT_MAIN_PUSH_RUN_ID`, `bundle_sha256=APPROVED_BUNDLE_SHA256`,
`rollback_ref=4769cf6de39012b0691027c35d33aef30edbef3a`, initially `dry_run=true`.
The workflow must run from the approved default-branch revision containing #354.
Even dry-run is a separately authorized future dispatch, not part of packaging.

Before any non-dry action, a separate user card must cover Quant-only helper/
restricted account/key/environment bootstrap, exact unit/drop-in originals,
the minimal compatible top-level owner config and its hash, complete 2.0.7 venv,
and old/new config plus DB-copy rollback compatibility. The baseline remains
`20261014-quant-v2.0.7-4769cf6`; no rollback has been exercised here. Quiescence
and pre/post-stop zero counts require fresh evidence in the approved window.
See the [deployment prerequisites and gates](../deploy/vps/ci-deploy/README.md).

Packaging and CI do not clear Quant OAuth/App admission, an approved synthetic
submitter/instance, native short-TTL active subscription, signed task/result or
delete/gone acceptance. An owner-wide live grant and real research remain separate approvals.

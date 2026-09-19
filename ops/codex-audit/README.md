# Codex audit runtime ops

## Model catalog auto-sync

Fully automatic monthly model tier maintenance:

- `scripts/sync_model_catalog.py` discovers OpenAI / Anthropic models via provider APIs
- writes `/var/lib/codex-audit-bridge/model_catalog.json` (or `MODEL_CATALOG_PATH`)
- `service/model_resolver.py` resolves task → tier → concrete model at runtime
- long-running workers reload when the on-disk catalog mtime changes
- VPS Codex service default `CODEX_AUDIT_SERVICE_MODEL=auto` resolves per call
  from that catalog (effort→tier); never pin a static deprecated model name in
  the adapter, and never forward `auto` to the Codex CLI. Auto selection only
  accepts the Codex research roster allowlist (`_CODEX_RESEARCH_MODELS`), so it
  refuses retired `gpt-5.4` / `gpt-5.4-mini`, Claude/Anthropic, and OpenAI
  API-only catalog ids; missing usable roster entries fail closed. Deploy with
  `CODEX_AUDIT_SERVICE_MODEL` set (including `auto`) rewrites the managed
  systemd drop-in and strips leftover MODEL pins from older drop-ins.

### Zero-touch VPS deploy

Repo secrets `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` are written to
`/etc/codex-audit-bridge/model-catalog.env` by the self-hosted workflow
[Deploy Model Catalog Sync](../../.github/workflows/deploy_model_catalog_sync.yml).
The same root-owned file is imported by `codex-audit-service`; no provider key
is placed in repository code, workflow output, or an inline systemd setting.

## Service-token storage

If the optional `CODEX_AUDIT_SERVICE_TOKEN` is configured for the dashboard's
read-only fallback, the deployment writes it to
`/etc/codex-audit-bridge/service-token.env` with mode `0600`. The systemd unit
references that file; it never embeds the token in the unit text. An OIDC
caller remains the required path for GitHub Actions, so this fallback must not
be copied into repository variables or workflow logs.

- **auto**: push to `main` that touches catalog paths triggers deploy
- **manual**: Actions → Deploy Model Catalog Sync → `deploy` / `inspect` / `sync-now`

Local equivalent (on the VPS runner host):

```bash
export OPENAI_API_KEY=... ANTHROPIC_API_KEY=...
bash ops/codex-audit/scripts/deploy_model_catalog_sync.sh deploy
```

Timer schedule: monthly on the 1st at 06:00 UTC (`model-catalog-sync.timer`).

## GitHub org-health token

Org-health can read its short-lived GitHub App installation token from
`/run/codex-org-health/installation-token.json`. The file is root-owned,
group-readable by `ubuntu`, and includes an `expires_at` value; an expired,
unsafe, or malformed file makes org-health unavailable before its cache is
consulted. The legacy dedicated `CODEX_AUDIT_SERVICE_GITHUB_TOKEN` remains a
fallback when no token-file path is configured. The generic `GITHUB_TOKEN` is
never used for this scope.

The protected `VPS Codex Service Ops` workflow has a manual
`install-org-health-token` mode on `main`. It stops the audit service after
checking `NoNewPrivileges`, installs the root-only issuer and systemd
refresh timer, passes the App private key through stdin, and validates the
first token without printing it. The job does not restart the audit service;
the 40-minute timer refreshes the token thereafter.

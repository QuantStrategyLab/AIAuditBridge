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
  accepts the Codex research roster allowlist, so it refuses retired
  `gpt-5.4` / `gpt-5.4-mini`, Claude/Anthropic, and OpenAI API-only catalog ids;
  missing usable roster entries fail closed. Explicit request/env models are
  preserved. Deploy with `CODEX_AUDIT_SERVICE_MODEL` set (including `auto`)
  rewrites the managed systemd drop-in and strips leftover MODEL pins from
  older drop-ins.

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

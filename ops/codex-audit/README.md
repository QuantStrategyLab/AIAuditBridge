# Codex audit runtime ops

## Model catalog auto-sync

Fully automatic monthly model tier maintenance:

- `scripts/sync_model_catalog.py` discovers OpenAI / Anthropic / Codex models
- writes `/var/lib/codex-audit-bridge/model_catalog.json` (or `MODEL_CATALOG_PATH`)
- `service/model_resolver.py` resolves task → tier → concrete model at runtime

Install timer on VPS:

```bash
sudo cp ops/codex-audit/systemd/model-catalog-sync.service.example /etc/systemd/system/model-catalog-sync.service
sudo cp ops/codex-audit/systemd/model-catalog-sync.timer.example /etc/systemd/system/model-catalog-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now model-catalog-sync.timer
```

Manual refresh:

```bash
python scripts/sync_model_catalog.py --force
```

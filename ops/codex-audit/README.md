# Codex audit runtime ops

## Model catalog auto-sync

Fully automatic monthly model tier maintenance:

- `scripts/sync_model_catalog.py` discovers OpenAI / Anthropic models via provider APIs
- writes `/var/lib/codex-audit-bridge/model_catalog.json` (or `MODEL_CATALOG_PATH`)
- `service/model_resolver.py` resolves task → tier → concrete model at runtime
- long-running workers reload when the on-disk catalog mtime changes

Install timer on VPS:

```bash
sudo mkdir -p /etc/codex-audit-bridge /var/lib/codex-audit-bridge
sudo tee /etc/codex-audit-bridge/model-catalog.env >/dev/null <<'EOF'
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
EOF
sudo chmod 600 /etc/codex-audit-bridge/model-catalog.env
sudo chown root:codex-audit /etc/codex-audit-bridge/model-catalog.env

sudo cp ops/codex-audit/systemd/model-catalog-sync.service.example /etc/systemd/system/model-catalog-sync.service
sudo cp ops/codex-audit/systemd/model-catalog-sync.timer.example /etc/systemd/system/model-catalog-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now model-catalog-sync.timer
```

Manual refresh:

```bash
set -a && source /etc/codex-audit-bridge/model-catalog.env && set +a
python scripts/sync_model_catalog.py --force
```

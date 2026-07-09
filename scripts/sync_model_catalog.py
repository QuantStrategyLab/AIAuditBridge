#!/usr/bin/env python3
"""Monthly auto-sync for the runtime model catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.model_catalog_sync import sync_catalog  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync auto-maintained model catalog.")
    parser.add_argument("--output", type=Path, help="Catalog output path.")
    parser.add_argument("--force", action="store_true", help="Ignore sync interval and rebuild now.")
    parser.add_argument("--json", action="store_true", help="Print resulting catalog JSON.")
    args = parser.parse_args()

    catalog = sync_catalog(
        output_path=str(args.output) if args.output else None,
        force=bool(args.force),
    )
    if args.json:
        print(json.dumps(catalog.to_dict(), indent=2, sort_keys=True))
    else:
        by_provider: dict[str, list[str]] = {}
        for model_id, record in sorted(catalog.models.items()):
            by_provider.setdefault(record.provider, []).append(model_id)
        top_models = sorted(
            catalog.models.values(),
            key=lambda item: (item.capability_score, item.model_id),
            reverse=True,
        )[:12]
        print(
            json.dumps(
                {
                    "synced_at": catalog.synced_at,
                    "catalog_source": catalog.catalog_source,
                    "tiers": {tier: spec.model for tier, spec in catalog.tiers.items()},
                    "deprecated": catalog.deprecated,
                    "inventory_counts": {
                        provider: len(models) for provider, models in sorted(by_provider.items())
                    },
                    "top_models": [
                        {
                            "model": item.model_id,
                            "provider": item.provider,
                            "score": round(float(item.capability_score), 4),
                        }
                        for item in top_models
                    ],
                    "has_gpt_5_6": any("5.6" in model_id for model_id in catalog.models),
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

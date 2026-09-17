"""Thin execution-adapter selection for Codex / Cursor CLI backends."""

from __future__ import annotations

from service.adapters.codex_adapter import CodexAdapter
from service.adapters.cursor_adapter import CursorAdapter
from service.contracts import PROVIDER_CODEX, PROVIDER_CURSOR


def resolve_execution_adapter(provider: str):
    """Return the CLI execution adapter for an admitted provider.

    Unknown providers fail closed. Empty provider defaults to Codex for
    backward-compatible sync/async execute paths that omit the field.
    """
    selected = str(provider or PROVIDER_CODEX).strip().lower() or PROVIDER_CODEX
    if selected == PROVIDER_CURSOR:
        return CursorAdapter()
    if selected == PROVIDER_CODEX:
        return CodexAdapter()
    raise ValueError(f"unsupported execution provider: {provider!r}")


__all__ = ["resolve_execution_adapter"]

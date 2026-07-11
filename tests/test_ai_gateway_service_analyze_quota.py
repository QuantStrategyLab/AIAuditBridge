from service.ai_gateway_service import _resolve_analyze_model


def test_analyze_model_resolves_codex_cli_to_api_backed_model() -> None:
    assert _resolve_analyze_model("codex-cli") == "claude-sonnet-4-6"
    assert _resolve_analyze_model("gpt-5.4-mini") == "gpt-5.4-mini"

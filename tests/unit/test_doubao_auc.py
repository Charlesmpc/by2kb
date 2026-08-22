from __future__ import annotations

from by2kb.providers.asr_doubao_auc import DoubaoAucConfig, _headers


def test_new_console_api_key_auth_does_not_require_legacy_credentials(monkeypatch):
    monkeypatch.setenv("VOLC_ACCESS_KEY_ID", "tos-ak")
    monkeypatch.setenv("VOLC_SECRET_ACCESS_KEY", "tos-sk")
    monkeypatch.setenv("TOS_BUCKET", "private-bucket")
    monkeypatch.setenv("DOUBAO_API_KEY", "new-api-key")
    monkeypatch.delenv("DOUBAO_APPID", raising=False)
    monkeypatch.delenv("DOUBAO_ACCESS_TOKEN", raising=False)

    config = DoubaoAucConfig.from_env()

    assert config.api_key == "new-api-key"
    assert config.app_id is None
    assert config.access_token is None


def test_new_console_api_key_headers_exclude_legacy_headers():
    config = DoubaoAucConfig(
        access_key="tos-ak",
        secret_key="tos-sk",
        bucket="private-bucket",
        api_key="new-api-key",
        app_id=None,
        access_token=None,
    )

    headers = _headers(config, "request-id", submit=True)

    assert headers["X-Api-Key"] == "new-api-key"
    assert headers["X-Api-Resource-Id"] == "volc.seedasr.auc"
    assert headers["X-Api-Sequence"] == "-1"
    assert "X-Api-App-Key" not in headers
    assert "X-Api-Access-Key" not in headers

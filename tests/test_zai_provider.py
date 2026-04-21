import pytest
from unittest.mock import patch


def test_zai_provider_importable():
    from tero2.providers.zai import ZaiProvider
    assert ZaiProvider is not None


def test_zai_provider_check_ready_without_key(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    with patch("tero2.providers.zai.SDK_AVAILABLE", True), \
         patch("tero2.providers.zai._read_settings_key", return_value=None):
        from tero2.providers.zai import ZaiProvider
        provider = ZaiProvider.__new__(ZaiProvider)
        ready, msg = provider.check_ready()
        assert not ready
        assert "ZAI_API_KEY" in msg or "key" in msg.lower()


def test_zai_registered_in_registry():
    from tero2.providers.registry import _REGISTRY
    assert "zai" in _REGISTRY

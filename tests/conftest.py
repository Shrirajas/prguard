"""Tests never touch the network: no real LLM, no real GitHub, no AgntID."""

import pytest

from backend.config import settings
from backend.events import bus


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "checker_provider", "none")
    monkeypatch.setattr(settings, "jev_api_key", "")
    monkeypatch.setattr(settings, "upstream", "mock")
    monkeypatch.setattr(settings, "enforcer", "LOCAL_POLICY")
    monkeypatch.setattr(settings, "target_repo", "you/prguard-demo-target")
    monkeypatch.setattr(settings, "other_repo", "you/prguard-demo-other")
    monkeypatch.setattr(settings, "runs_dir", tmp_path)
    bus.clear()

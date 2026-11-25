"""Exercises the Claude tool-use loop with a scripted fake client (no network, no cost)."""
from types import SimpleNamespace as NS

import anthropic

from app.ai import assistant
from app.db import SessionLocal
from app.services.common import sim_now


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
        if len(self.calls) == 1:
            return NS(stop_reason="tool_use", content=[
                NS(type="text", text="Checking."),
                NS(type="tool_use", id="tu_1", name="material_runout", input={"factory": "KNV", "days": 5})])
        tool_result = kw["messages"][-1]["content"][0]
        assert tool_result["tool_use_id"] == "tu_1" and "F-104" in tool_result["content"]
        return NS(stop_reason="end_turn", content=[NS(type="text", text="F-104 at Kernersville may run out tomorrow.")])


def test_llm_tool_loop(monkeypatch, client=None):
    from app.main import migrate, ensure_seeded
    migrate()
    ensure_seeded()
    fake = FakeMessages()
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: NS(beta=NS(messages=fake)))
    monkeypatch.setattr(assistant.get_settings(), "llm_provider", "anthropic")
    db = SessionLocal()
    try:
        res = assistant.answer(db, "Which materials may run out?", "KNV", sim_now(db))
    finally:
        db.close()
    assert res["mode"] == "anthropic" and "F-104" in res["answer"]
    assert res["sources"] == ["material_runout"] and res["charts"]
    assert len(fake.calls) == 2


def test_llm_failure_falls_back(monkeypatch):
    def boom(*a, **k):
        raise anthropic.APIConnectionError(request=None)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: NS(beta=NS(messages=NS(create=boom))))
    monkeypatch.setattr(assistant.get_settings(), "llm_provider", "anthropic")
    db = SessionLocal()
    try:
        res = assistant.answer(db, "How many cartons failed inspection today?", "ALL", sim_now(db))
    finally:
        db.close()
    assert res["mode"] == "mock" and "LLM unavailable" in res["note"] and "carton_failures" in res["sources"]

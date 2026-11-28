"""#451: direct tests for the find_files tool's actions and permission level."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from alice.tools import findfiles as ff
from alice.tools.base import COMPUTER, READ, WRITE, ToolError


def _ctx(tmp_path):
    return SimpleNamespace(cwd=tmp_path, resolve=lambda value: (tmp_path / value).resolve())


def test_search_returns_formatted_hits_with_the_requested_arguments(monkeypatch, tmp_path):
    seen = {}
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    stamp = time.time()

    def fake_search(query="", kind="", days=None, limit=20):
        seen.update(query=query, kind=kind, days=days)
        return [(path, stamp)]

    monkeypatch.setattr(ff, "search", fake_search)
    result = ff.FindFiles().run(_ctx(tmp_path), "search", "notes", "metin", 7)
    assert seen == {"query": "notes", "kind": "metin", "days": 7}
    assert "notes.txt" in result.text and not result.is_error


def test_recent_falls_back_to_search_when_there_are_no_recent_files(monkeypatch, tmp_path):
    called = []

    def fake_search(query="", kind="", days=None, limit=20):
        called.append(query)
        return []

    monkeypatch.setattr(ff, "recent", lambda *a, **k: [])
    monkeypatch.setattr(ff, "search", fake_search)
    result = ff.FindFiles().run(_ctx(tmp_path), "recent", "contract")
    assert called == ["contract"]
    assert result.text == "not found"


def test_downloads_returns_not_found_without_hits(monkeypatch, tmp_path):
    monkeypatch.setattr(ff, "downloads", lambda *a, **k: [])
    assert ff.FindFiles().run(_ctx(tmp_path), "downloads").text == "not found"


def test_open_uses_the_chat_folder_and_refuses_a_missing_file(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(ff.os, "startfile", lambda value: opened.append(value), raising=False)
    (tmp_path / "given.pdf").write_bytes(b"x")
    tool = ff.FindFiles()
    with pytest.raises(ToolError):
        tool.run(_ctx(tmp_path), "open", path="missing.pdf")
    assert tool.run(_ctx(tmp_path), "open", path="given.pdf").text == "opened: given.pdf"
    assert opened == [str((tmp_path / "given.pdf").resolve())]


def test_tidy_describes_the_plan_then_moves_only_when_approved(monkeypatch, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    source = downloads / "fatura.pdf"
    source.write_bytes(b"x")
    dest = downloads / "Belgeler" / "fatura.pdf"
    monkeypatch.setattr(ff, "tidy_plan", lambda folder=None: [(source, dest)])
    tool = ff.FindFiles()
    plan = tool.run(_ctx(tmp_path), "tidy")
    assert "1 files will be moved" in plan.text and source.exists()
    moved = tool.run(_ctx(tmp_path), "tidy", apply=True)
    assert "1 files moved" in moved.text
    assert dest.is_file() and not source.exists()


def test_unknown_action_is_reported(monkeypatch, tmp_path):
    with pytest.raises(ToolError, match="unknown action"):
        ff.FindFiles().run(_ctx(tmp_path), "delete")


def test_tidy_with_apply_needs_the_higher_permission():
    tool = ff.FindFiles()
    assert tool.risk_for({"action": "tidy", "apply": True}) == WRITE
    assert tool.risk_for({"action": "tidy"}) == READ
    assert tool.risk_for({"action": "open"}) == COMPUTER
    assert tool.risk_for({"action": "search"}) == READ

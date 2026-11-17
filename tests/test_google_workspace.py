"""Google Drive, Docs and Sheets connection: multiple servers behind a single sign-in, nested body templates."""

from __future__ import annotations

import pytest

from alice.connect import runner
from alice.connect.manifest import discover
from tests.conftest import make_config


@pytest.fixture
def ws(monkeypatch):
    monkeypatch.setattr(runner, "token_for", lambda m: "T")
    return discover(make_config({"brain": ["a:m"]}))[0]["google_workspace"]


def test_actions_go_to_the_right_google_servers(ws):
    r = runner.build(ws, ws.actions["drive_search"], {"q": "name contains 'rapor'"})
    assert r["url"] == "https://www.googleapis.com/drive/v3/files" and r["params"]["q"] == "name contains 'rapor'"
    assert r["headers"]["Authorization"] == "Bearer T"
    r = runner.build(ws, ws.actions["drive_read"], {"fileId": "F1"})
    assert r["url"].endswith("/drive/v3/files/F1/export") and r["params"]["mimeType"] == "text/plain"
    r = runner.build(ws, ws.actions["sheets_read"], {"spreadsheetId": "S", "range": "Sayfa1!A1:B2"})
    assert r["url"].startswith("https://sheets.googleapis.com/v4/spreadsheets/S/values/Sayfa1")


def test_nested_bodies_are_filled_from_templates(ws):
    r = runner.build(ws, ws.actions["docs_append"], {"documentId": "D1", "text": "\nmerhaba"})
    assert r["url"] == "https://docs.googleapis.com/v1/documents/D1:batchUpdate"
    assert r["json"] == {"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": "\nmerhaba"}}]}
    r = runner.build(ws, ws.actions["sheets_create"], {"title": "Bütçe"})
    assert r["json"] == {"properties": {"title": "Bütçe"}}
    r = runner.build(ws, ws.actions["sheets_append"], {"spreadsheetId": "S", "range": "A1", "values": [["a", 1]]})
    assert r["json"] == {"values": [["a", 1]]} and r["params"]["valueInputOption"] == "USER_ENTERED"
    assert ws.actions["sheets_append"].writes and not ws.actions["sheets_read"].writes


def test_template_with_missing_optional_value_drops_the_key():
    assert runner._fill({"a": "{x}", "b": [{"c": "{y}"}], "d": 1}, {"x": 5}) == {"a": 5, "b": [{}], "d": 1}

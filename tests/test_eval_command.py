"""#450: the `alice eval` command itself, with no model and no network."""

from __future__ import annotations

import time

from alice import evals


def _outcomes(cases, passed):
    return [
        evals.Outcome(case, ok, "reply", [], [] if ok else ["reason"], 0.1, "fake-model")
        for case, ok in zip(cases, passed)
    ]


def test_add_writes_a_case_and_reports_the_path(monkeypatch, capsys, tmp_path):
    called = {}

    def fake_add(sayings, note="", path=None):
        called.update(sayings=sayings, note=note)
        return 1

    monkeypatch.setattr(evals, "evals_path", lambda: tmp_path / "evals.toml")
    monkeypatch.setattr(evals, "add_cases", fake_add)
    assert evals.eval_cmd(["--add", "what time is it"]) == 0
    assert called == {"sayings": ["what time is it"], "note": ""}
    assert "vaka eklendi" in capsys.readouterr().out


def test_from_log_uses_the_log_sayings_and_note(monkeypatch, capsys, tmp_path):
    called = {}

    def fake_add(sayings, note="", path=None):
        called.update(sayings=sayings, note=note)
        return 2

    monkeypatch.setattr(evals, "evals_path", lambda: tmp_path / "evals.toml")
    monkeypatch.setattr(evals, "sayings_from_log", lambda n: ["first", "second"])
    monkeypatch.setattr(evals, "add_cases", fake_add)
    assert evals.eval_cmd(["--from-log", "3"]) == 0
    assert called == {"sayings": ["first", "second"], "note": "voice.log'dan"}
    assert "2 vaka eklendi" in capsys.readouterr().out


def test_only_filters_cases_by_name_or_sentence(monkeypatch, capsys):
    cases = [evals.Case("weather today", name="weather"), evals.Case("time now", name="time")]
    seen = {}

    def fake_run_all(cases, on_case=lambda i, o: None, stop=lambda: False, cfg=None):
        seen["cases"] = cases
        return _outcomes(cases, [True] * len(cases))

    monkeypatch.setattr(evals, "load_cases", lambda: cases)
    monkeypatch.setattr(evals, "run_all", fake_run_all)
    monkeypatch.setattr(evals, "save_results", lambda outcomes: {"before": None, "before_total": None, "fixed": [], "broken": []})
    assert evals.eval_cmd(["--only", "WEATHER"]) == 0
    assert [c.say for c in seen["cases"]] == ["weather today"]


def test_no_cases_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(evals, "load_cases", lambda: [])
    monkeypatch.setattr(evals, "evals_path", lambda: "evals.toml")
    assert evals.eval_cmd([]) == 1
    assert "no cases" in capsys.readouterr().out


def test_feedback_without_rows_says_no_feedback(monkeypatch, capsys):
    monkeypatch.setattr(evals, "recent_feedback", lambda n: [])
    assert evals.eval_cmd(["--feedback"]) == 0
    assert "no feedback" in capsys.readouterr().out


def test_feedback_prints_recent_disliked_answers(monkeypatch, capsys):
    rows = [{"ts": time.time(), "say": "why", "reply": "because", "source": "app", "note": "too long"}]
    monkeypatch.setattr(evals, "recent_feedback", lambda n: rows)
    assert evals.eval_cmd(["--feedback"]) == 0
    out = capsys.readouterr().out
    assert "why" in out and "because" in out and "too long" in out


def test_show_prints_a_pass_fail_table_and_summary(monkeypatch, capsys):
    cases = [evals.Case("one", name="one"), evals.Case("two", name="two")]

    def fake_run_all(cases, on_case=lambda i, o: None, stop=lambda: False, cfg=None):
        outcomes = _outcomes(cases, [True, False])
        for i, outcome in enumerate(outcomes, 1):
            on_case(i, outcome)
        return outcomes

    monkeypatch.setattr(evals, "load_cases", lambda: cases)
    monkeypatch.setattr(evals, "run_all", fake_run_all)
    monkeypatch.setattr(
        evals,
        "save_results",
        lambda outcomes: {"before": 1, "before_total": 2, "fixed": [], "broken": ["two"]},
    )
    assert evals.eval_cmd([]) == 1
    out = capsys.readouterr().out
    assert "passed" in out and "did not pass" in out
    assert "1/2 passed" in out
    assert "broke: two" in out

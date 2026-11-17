"""Proxy: keep the same conversation on the same account (each account has its own prompt cache)."""

from alice.app.cliproxy import ensure_affinity


def test_adds_affinity_under_routing(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text('port: 8317\nrouting:\n  strategy: "round-robin"\naccess:\n  api-keys: []\n', encoding="utf-8")
    assert ensure_affinity(p)
    text = p.read_text(encoding="utf-8")
    assert 'routing:\n  session-affinity: true\n  strategy: "round-robin"\n' in text
    assert not ensure_affinity(p)  # does not touch it the second time


def test_respects_explicit_setting_and_missing_routing(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("routing:\n  session-affinity: false\n", encoding="utf-8")
    assert not ensure_affinity(p) and "false" in p.read_text(encoding="utf-8")
    q = tmp_path / "b.yaml"
    q.write_text("port: 8317\n", encoding="utf-8")
    assert ensure_affinity(q) and q.read_text(encoding="utf-8").endswith("routing:\n  session-affinity: true\n")

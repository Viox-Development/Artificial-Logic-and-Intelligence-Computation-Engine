"""A project's .alice/permissions.toml cannot grant itself permission: allow rules count only as ALICE wrote them."""
from __future__ import annotations

from alice import permissions as perm


def write(workdir, text):
    p = workdir / ".alice" / "permissions.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    perm._project_cache.clear()  # noqa: SLF001 - a file rewritten in the same second may keep its mtime
    return p


def test_cloned_repo_rules_do_not_grant_permissions(workdir):
    write(workdir, '[permissions]\nallow = ["*(*)", "run_command"]\ndeny = ["run_command(rm*)"]\n')
    rules = perm.project_rules(workdir)
    assert rules["allow"] == [] and rules["deny"] == ["run_command(rm*)"]  # deny/ask tighten: these apply
    assert perm.project_rules(workdir, include_untrusted=True)["allow"] == ["*(*)", "run_command"]
    assert not perm.project_rules_trusted(workdir)


def test_alice_written_rules_apply_until_tampered(workdir):
    perm.persist_allow(workdir, "run_command(git status*)")
    assert perm.project_rules(workdir)["allow"] == ["run_command(git status*)"]
    p = workdir / ".alice" / "permissions.toml"
    write(workdir, p.read_text(encoding="utf-8").replace('"run_command(git status*)"', '"*(*)"'))
    assert perm.project_rules(workdir)["allow"] == []  # changed from outside: approval required
    perm.persist_allow(workdir, "run_command(npm test*)")  # one approval does not launder a fake rule
    assert perm.project_rules(workdir)["allow"] == ["run_command(npm test*)"]


def test_user_can_trust_and_remove(workdir):
    write(workdir, '[permissions]\nallow = ["run_command(make*)"]\n')
    assert perm.trust_project_rules(workdir)
    assert perm.project_rules(workdir)["allow"] == ["run_command(make*)"]
    assert perm.remove_project_rule(workdir, "allow", "run_command(make*)")
    assert perm.project_rules(workdir, include_untrusted=True)["allow"] == []


def test_permissions_apply_always_asks():
    from alice.tools.base import DANGER
    from alice.tools.permshare import PermissionsTool

    assert PermissionsTool().risk_for({"action": "apply"}) == DANGER

"""GitHub Actions workflow audit (alice/workflow_audit.py) and how it connects to review (review.workflow_findings)."""

from __future__ import annotations

from alice import review, workflow_audit

BAD = """name: ci
on:
  pull_request_target:
    types: [opened]
permissions: write-all
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: someone/tool@v2
      - uses: someone/pinned@0123456789abcdef0123456789abcdef01234567
      - name: selam
        run: |
          echo "baslik: ${{ github.event.pull_request.title }}"
          echo "numara: ${{ github.event.pull_request.number }}"
      - run: echo ${{ github.head_ref }}
      - uses: actions/github-script@v7
        with:
          script: |
            const body = `${{ github.event.comment.body }}`
      # - run: echo ${{ github.event.issue.title }}   (yorum: denetlenmez)
"""

SAFE = """on: [pull_request]
permissions:
  contents: read
jobs:
  t:
    runs-on: ubuntu-latest
    env:
      TITLE: ${{ github.event.pull_request.title }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: echo "$TITLE"
"""


def by_line(text: str) -> dict[int, str]:
    return {i.line: i.severity for i in workflow_audit.audit(text)}


def test_flags_known_dangerous_patterns():
    got = by_line(BAD)
    assert got[5] == "medium"  # write-all
    assert got[12] == "critical"  # pull_request_target + PR head checkout
    assert got[13] == "low"  # someone/tool@v2 not pinned
    assert 14 not in got  # pinned to a SHA: no issue
    assert 10 not in got  # actions/* is a trusted owner
    assert got[17] == "high"  # PR title inside run
    assert 18 not in got  # number: a plain number, an attacker cannot write it
    assert got[19] == "high"  # single-line run: head_ref
    assert got[23] == "high"  # comment body inside github-script
    assert 24 not in got  # comment line


def test_safe_workflow_has_no_issues():
    # pull_request (no extra privileges) + quoted use via env: a safe pattern.
    assert workflow_audit.audit(SAFE) == []


def test_bracket_trigger_list_counts_as_privileged():
    text = "on: [push, pull_request_target]\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n" \
           "        with:\n          ref: ${{ github.head_ref }}\n"
    assert by_line(text) == {7: "critical"}


def test_is_workflow():
    assert workflow_audit.is_workflow(".github/workflows/ci.yml")
    assert workflow_audit.is_workflow("sub/.github/workflows/x.yaml")
    assert not workflow_audit.is_workflow("docs/workflows/ci.yml")


def test_review_reports_only_added_lines_that_match_local_file(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(BAD, encoding="utf-8")
    lines = BAD.splitlines()
    # The diff only adds line 17 (the PR title inside run); lines 5 and 12 have been there all along: not reported.
    diff = ("diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
            "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
            "@@ -16,1 +16,2 @@\n " + lines[15] + "\n+" + lines[16] + "\n")
    found = review.workflow_findings(tmp_path, diff)
    assert [(f.line, f.severity, f.verdict) for f in found] == [(17, "high", "KURAL")]
    # If the local file differs from the diff (another branch open), skip it so it never lands on the wrong line.
    (wf / "ci.yml").write_text(BAD.replace("pull_request.title", "pull_request.number"), encoding="utf-8")
    assert review.workflow_findings(tmp_path, diff) == []


def test_deep_review_runs_each_focus_merges_and_survives_a_failing_reviewer(monkeypatch, tmp_path):
    seen = []

    def fake_one(cfg, router, cwd, title, diff, focus):
        seen.append(focus)
        if focus == "performance":
            raise RuntimeError("model yok")
        if focus == "bugs":
            return [review.Finding("a.py", 10, "medium", "Yarış durumu!", "d")]
        if focus == "security":  # same issue one line away but higher severity: this one stays
            return [review.Finding("a.py", 11, "high", "yarış durumu", "d")]
        return [review.Finding("a.py", 40, "low", "test yok", "d")]

    monkeypatch.setattr(review, "_review_one", fake_one)
    found = review.review(None, None, tmp_path, "t", "", focus="deep", verify=False)
    assert sorted(seen) == sorted(review.DEEP)
    assert [(f.line, f.severity) for f in found] == [(11, "high"), (40, "low")]

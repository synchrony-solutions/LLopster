"""Tests for the GitHub client — no live HTTP calls."""

import base64
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.agent.alert_handler import ParsedAlert
from src.agent.patch_generator import PatchProposal
from src.integrations.github_client import (
    GitHubClient,
    PatchApplyError,
    PatchValidationError,
    ProtectedPathError,
    _apply_patch,
    _extract_diff,
    _is_protected_path,
    _parse_file_patches,
    _make_branch_name,
    _pr_body,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DIFF = """\
--- a/helm-values.yaml
+++ b/helm-values.yaml
@@ -2,4 +2,4 @@
 resources:
   limits:
-    memory: "512MBz"
+    memory: "512Mi"
     cpu: "500m"
"""

SAMPLE_PROPOSAL_TEXT = f"""\
## Root Cause
The memory unit is invalid.

## Proposed Patch
```diff
{SAMPLE_DIFF}```

## Reasoning
Replacing MBz with Mi fixes the validator.
"""

NO_PATCH_TEXT = """\
## Root Cause
Synthetic alert.

## Proposed Patch
No code patch is appropriate for this alert.

```diff
(no changes)
```

## Reasoning
Always-firing alert, nothing to fix.
"""

ORIGINAL_YAML = """\
# Helm values for demo-app deployment.
resources:
  limits:
    memory: "512MBz"
    cpu: "500m"
requests:
  memory: "256Mi"
"""


def _make_alert() -> ParsedAlert:
    return ParsedAlert(
        fingerprint="abc",
        status="firing",
        alertname="HelmValuesMisconfigured",
        severity="warning",
        service="demo-app",
        summary="bad memory unit",
        description="512MBz is invalid",
        starts_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": "demo-app"},
        annotations={},
        generator_url="",
    )


def _make_proposal(text: str = SAMPLE_PROPOSAL_TEXT) -> PatchProposal:
    return PatchProposal(
        text=text,
        model="claude-opus-4-7",
        input_tokens=11006,
        output_tokens=672,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        confidence=5,
        confidence_reason="Root cause is unambiguous.",
    )


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

def test_extract_diff_strips_fences():
    diff = _extract_diff(SAMPLE_PROPOSAL_TEXT)
    assert "--- a/helm-values.yaml" in diff
    assert "```" not in diff


def test_extract_diff_empty_for_no_patch():
    diff = _extract_diff(NO_PATCH_TEXT)
    # (no changes) content is returned but produces no parseable patches
    patches = _parse_file_patches(diff)
    assert patches == {}


def test_parse_file_patches_extracts_path_and_hunks():
    diff = _extract_diff(SAMPLE_PROPOSAL_TEXT)
    patches = _parse_file_patches(diff)
    assert "helm-values.yaml" in patches
    hunks = patches["helm-values.yaml"]
    assert len(hunks) == 1
    old_start, hunk_lines = hunks[0]
    assert old_start == 2
    assert any(l.startswith("-") and "512MBz" in l for l in hunk_lines)
    assert any(l.startswith("+") and "512Mi" in l for l in hunk_lines)


def test_parse_file_patches_strips_b_prefix():
    diff = _extract_diff(SAMPLE_PROPOSAL_TEXT)
    patches = _parse_file_patches(diff)
    # Path should NOT start with b/
    for path in patches:
        assert not path.startswith("b/")


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def test_apply_patch_replaces_bad_memory():
    diff = _extract_diff(SAMPLE_PROPOSAL_TEXT)
    patches = _parse_file_patches(diff)
    hunks = patches["helm-values.yaml"]
    result = _apply_patch(ORIGINAL_YAML, hunks)
    assert "512Mi" in result
    assert "512MBz" not in result


def test_apply_patch_preserves_surrounding_lines():
    diff = _extract_diff(SAMPLE_PROPOSAL_TEXT)
    patches = _parse_file_patches(diff)
    hunks = patches["helm-values.yaml"]
    result = _apply_patch(ORIGINAL_YAML, hunks)
    assert "256Mi" in result
    assert "500m" in result
    assert "# Helm values" in result


# ---------------------------------------------------------------------------
# Patch application — adversarial / fail-closed (the field failure mode)
# ---------------------------------------------------------------------------

# Same correct change as SAMPLE_DIFF, but the @@ header points two lines too
# low. The anchor (context + removal) still matches uniquely at the real
# location, so the drift fallback relocates the hunk there instead of failing.
OFF_BY_N_DIFF = """\
--- a/helm-values.yaml
+++ b/helm-values.yaml
@@ -4,4 +4,4 @@
 resources:
   limits:
-    memory: "512MBz"
+    memory: "512Mi"
     cpu: "500m"
"""

# Correct line number, but a context line that does not exist in the file.
BAD_CONTEXT_DIFF = """\
--- a/helm-values.yaml
+++ b/helm-values.yaml
@@ -2,4 +2,4 @@
 resources:
   thresholds:
-    memory: "512MBz"
+    memory: "512Mi"
     cpu: "500m"
"""

# Correct line number + context, but the "-" line claims content the file does
# not actually have (a drifted file / hallucinated removal).
BAD_REMOVAL_DIFF = """\
--- a/helm-values.yaml
+++ b/helm-values.yaml
@@ -2,4 +2,4 @@
 resources:
   limits:
-    memory: "999MBz"
+    memory: "512Mi"
     cpu: "500m"
"""

MULTI_HUNK_ORIGINAL = """\
alpha: 1
beta: 2
gamma: 3
delta: 4
epsilon: 5
zeta: 6
"""

# First hunk is correct; the second hunk's context line is wrong.
MULTI_HUNK_ONE_BAD_DIFF = """\
--- a/conf.yaml
+++ b/conf.yaml
@@ -1,2 +1,2 @@
-alpha: 1
+alpha: 100
 beta: 2
@@ -5,2 +5,2 @@
-WRONG: 5
+epsilon: 500
 zeta: 6
"""


def test_apply_patch_relocates_off_by_n_header():
    """Drift fallback: a stale @@ header whose anchor matches uniquely elsewhere
    relocates and applies correctly instead of failing to zero PRs."""
    hunks = _parse_file_patches(OFF_BY_N_DIFF)["helm-values.yaml"]
    result = _apply_patch(ORIGINAL_YAML, hunks)
    assert '    memory: "512Mi"' in result
    assert '512MBz' not in result
    # The unrelated `requests: memory: "256Mi"` block is untouched.
    assert '  memory: "256Mi"' in result


def test_apply_patch_relocates_to_nearest_when_anchor_repeats():
    """When the anchor matches in several places, the hunk lands at the offset
    nearest the header (the diff's line number disambiguates repeats)."""
    original = (
        "block:\n"
        "  value: 1\n"      # line 2 — first identical block
        "middle:\n"
        "  keep: yes\n"
        "block:\n"
        "  value: 1\n"      # line 6 — second identical block
        "tail:\n"
    )
    # Header points at line 5 (the second block); nearest match is that one.
    hunks = [(5, ["-  value: 1", "+  value: 2"])]
    result = _apply_patch(original, hunks)
    # Second block changed, first block preserved.
    assert result.splitlines()[1] == "  value: 1"   # first block untouched
    assert result.splitlines()[5] == "  value: 2"   # second block edited


def test_apply_patch_no_relocated_match_still_fails_closed():
    """A hallucinated context/removal line that matches nowhere fails closed —
    the drift fallback never invents a location."""
    hunks = _parse_file_patches(BAD_CONTEXT_DIFF)["helm-values.yaml"]
    with pytest.raises(PatchApplyError):
        _apply_patch(ORIGINAL_YAML, hunks)


def test_apply_patch_rejects_non_matching_context_line():
    hunks = _parse_file_patches(BAD_CONTEXT_DIFF)["helm-values.yaml"]
    with pytest.raises(PatchApplyError):
        _apply_patch(ORIGINAL_YAML, hunks)


def test_apply_patch_rejects_non_matching_removal_line():
    hunks = _parse_file_patches(BAD_REMOVAL_DIFF)["helm-values.yaml"]
    with pytest.raises(PatchApplyError):
        _apply_patch(ORIGINAL_YAML, hunks)


def test_apply_patch_rejects_header_past_eof():
    hunks = [(999, [" resources:", "-    x", "+    y"])]
    with pytest.raises(PatchApplyError):
        _apply_patch(ORIGINAL_YAML, hunks)


def test_apply_patch_multi_hunk_one_bad_aborts_whole_patch():
    hunks = _parse_file_patches(MULTI_HUNK_ONE_BAD_DIFF)["conf.yaml"]
    assert len(hunks) == 2  # both hunks parsed
    with pytest.raises(PatchApplyError):
        _apply_patch(MULTI_HUNK_ORIGINAL, hunks)


def test_apply_patch_multi_hunk_all_good_applies():
    diff = """\
--- a/conf.yaml
+++ b/conf.yaml
@@ -1,2 +1,2 @@
-alpha: 1
+alpha: 100
 beta: 2
@@ -5,2 +5,2 @@
-epsilon: 5
+epsilon: 500
 zeta: 6
"""
    hunks = _parse_file_patches(diff)["conf.yaml"]
    result = _apply_patch(MULTI_HUNK_ORIGINAL, hunks)
    assert "alpha: 100" in result
    assert "epsilon: 500" in result
    assert "gamma: 3" in result  # untouched middle preserved
    assert "alpha: 1\n" not in result


# ---------------------------------------------------------------------------
# Branch name
# ---------------------------------------------------------------------------

def test_make_branch_name_format():
    alert = _make_alert()
    name = _make_branch_name(alert)
    assert name.startswith("llopster/helmvaluesmisconfigured-")
    assert "/" in name


# ---------------------------------------------------------------------------
# PR body
# ---------------------------------------------------------------------------

def test_pr_body_contains_key_sections():
    body = _pr_body(_make_alert(), _make_proposal())
    assert "HelmValuesMisconfigured" in body
    assert "Root Cause" in body
    assert "Reasoning" in body
    assert "claude-opus-4-7" in body


# ---------------------------------------------------------------------------
# GitHubClient API flow
# ---------------------------------------------------------------------------

def _make_mock_http(file_content: str = ORIGINAL_YAML) -> AsyncMock:
    """Return a mock AsyncClient whose post() / get() / put() return
    realistic GitHub API shapes in the order the client calls them."""
    encoded = base64.b64encode(file_content.encode()).decode()

    repo_resp = MagicMock()
    repo_resp.raise_for_status = MagicMock()
    repo_resp.json = MagicMock(return_value={"default_branch": "main"})

    ref_resp = MagicMock()
    ref_resp.raise_for_status = MagicMock()
    ref_resp.json = MagicMock(return_value={"object": {"sha": "deadbeef"}})

    create_branch_resp = MagicMock()
    create_branch_resp.raise_for_status = MagicMock()
    create_branch_resp.json = MagicMock(return_value={})

    file_resp = MagicMock()
    file_resp.raise_for_status = MagicMock()
    file_resp.json = MagicMock(return_value={"content": encoded, "sha": "fileshaabc"})

    update_resp = MagicMock()
    update_resp.raise_for_status = MagicMock()
    update_resp.json = MagicMock(return_value={})

    pr_resp = MagicMock()
    pr_resp.raise_for_status = MagicMock()
    pr_resp.json = MagicMock(return_value={
        "number": 42,
        "html_url": "https://github.com/owner/repo/pull/42",
    })

    mock = AsyncMock()
    # GET calls in order: repo info, ref SHA, file content
    mock.get = AsyncMock(side_effect=[repo_resp, ref_resp, file_resp])
    # POST calls in order: create branch, create PR
    mock.post = AsyncMock(side_effect=[create_branch_resp, pr_resp])
    # PUT call: update file
    mock.put = AsyncMock(return_value=update_resp)
    return mock


@pytest.mark.asyncio
async def test_open_pr_returns_pull_request():
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)
    pr = await client.open_pr(_make_alert(), _make_proposal(), repo="owner/repo")
    assert pr.number == 42
    assert pr.url == "https://github.com/owner/repo/pull/42"
    assert pr.branch.startswith("llopster/")


@pytest.mark.asyncio
async def test_open_pr_creates_branch_before_pr():
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)
    await client.open_pr(_make_alert(), _make_proposal(), repo="owner/repo")

    # First POST = create branch, second POST = create PR
    first_post_url = mock_http.post.call_args_list[0].args[0]
    second_post_url = mock_http.post.call_args_list[1].args[0]
    assert first_post_url.endswith("/git/refs")
    assert second_post_url.endswith("/pulls")


@pytest.mark.asyncio
async def test_open_pr_defaults_to_draft():
    """Least-privilege posture: an LLM-authored PR opens as a draft by default."""
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)
    await client.open_pr(_make_alert(), _make_proposal(), repo="owner/repo")

    pr_call = mock_http.post.call_args_list[1]
    assert pr_call.kwargs["json"]["draft"] is True


@pytest.mark.asyncio
async def test_open_pr_draft_opt_out():
    """draft=False (the opt-out) opens a ready-for-review PR."""
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)
    await client.open_pr(_make_alert(), _make_proposal(), repo="owner/repo", draft=False)

    pr_call = mock_http.post.call_args_list[1]
    assert pr_call.kwargs["json"]["draft"] is False


@pytest.mark.asyncio
async def test_open_pr_applies_patch_in_file_update():
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)
    await client.open_pr(_make_alert(), _make_proposal(), repo="owner/repo")

    put_call = mock_http.put.call_args
    sent_content = base64.b64decode(put_call.kwargs["json"]["content"]).decode()
    assert "512Mi" in sent_content
    assert "512MBz" not in sent_content


@pytest.mark.asyncio
async def test_open_pr_raises_when_no_parseable_diff():
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)
    with pytest.raises(ValueError, match="no parseable file patches"):
        await client.open_pr(_make_alert(), _make_proposal(NO_PATCH_TEXT), repo="owner/repo")


@pytest.mark.asyncio
async def test_open_pr_fails_closed_on_context_mismatch():
    """A diff whose anchor matches nowhere in the real file (a hallucinated
    context line the drift fallback can't relocate) must abort BEFORE any
    write — no branch created, no file committed, no PR opened."""
    bad_proposal_text = f"""\
## Root Cause
The memory unit is invalid.

## Proposed Patch
```diff
{BAD_CONTEXT_DIFF}```

## Reasoning
Replacing MBz with Mi fixes the validator.
"""
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)

    with pytest.raises(PatchApplyError, match="helm-values.yaml"):
        await client.open_pr(_make_alert(), _make_proposal(bad_proposal_text), repo="owner/repo")

    # No mutating call should have fired: file was fetched (GET) but the branch
    # (POST /git/refs), the commit (PUT), and the PR (POST /pulls) never happen.
    mock_http.put.assert_not_called()
    post_urls = [c.args[0] for c in mock_http.post.call_args_list]
    assert post_urls == []


# ---------------------------------------------------------------------------
# Path gates: protected execution/deploy surfaces + affected_files allowlist
# ---------------------------------------------------------------------------

WORKFLOW_DIFF = """\
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,2 +1,2 @@
 name: ci
-on: push
+on: [push, pull_request]
"""

WORKFLOW_PROPOSAL_TEXT = f"""\
## Root Cause
CI trigger is too narrow.

## Proposed Patch
```diff
{WORKFLOW_DIFF}```

## Reasoning
Broaden the trigger.
"""


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/actions/foo/action.yml",
        "Dockerfile",
        "app/Dockerfile.prod",
        "Containerfile",
        "docker-compose.yml",
        "docker-compose.override.yaml",
        "helm-chart/templates/agent.yaml",
        "charts/app/values.yaml",
        ".circleci/config.yml",
        "Jenkinsfile",
        ".gitlab-ci.yml",
        "../outside/repo.py",
        "",
    ],
)
def test_is_protected_path_denies_execution_surfaces(path):
    assert _is_protected_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "helm-values.yaml",          # app config, a legit seeded patch target
        "check_db_pool.py",
        "src/agent/processor.py",
        "config/settings.yaml",
        "./app/main.py",
    ],
)
def test_is_protected_path_allows_normal_source(path):
    assert _is_protected_path(path) is False


@pytest.mark.asyncio
async def test_open_pr_refuses_protected_path_before_any_call():
    """A diff targeting .github/workflows/* is refused fail-closed, before even
    fetching the default branch — no GET, no branch, no commit, no PR."""
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)

    with pytest.raises(ProtectedPathError, match=r"\.github/workflows/ci\.yml"):
        await client.open_pr(
            _make_alert(), _make_proposal(WORKFLOW_PROPOSAL_TEXT), repo="owner/repo",
        )

    mock_http.get.assert_not_called()
    mock_http.post.assert_not_called()
    mock_http.put.assert_not_called()


@pytest.mark.asyncio
async def test_open_pr_refuses_path_outside_affected_files_allowlist():
    """When the investigation supplies an allowlist, a hunk targeting a file
    outside it fails the run closed — nothing is written."""
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)

    with pytest.raises(ProtectedPathError, match="allowlist"):
        await client.open_pr(
            _make_alert(), _make_proposal(), repo="owner/repo",
            allowed_paths={"some/other/file.py"},
        )

    mock_http.get.assert_not_called()
    mock_http.post.assert_not_called()
    mock_http.put.assert_not_called()


@pytest.mark.asyncio
async def test_open_pr_allows_path_in_affected_files_allowlist():
    """A hunk targeting a file that IS in the allowlist proceeds normally."""
    mock_http = _make_mock_http()
    client = GitHubClient(token="gh-token", client=mock_http)

    pr = await client.open_pr(
        _make_alert(), _make_proposal(), repo="owner/repo",
        allowed_paths={"helm-values.yaml"},
    )
    assert pr.number == 42


# ---------------------------------------------------------------------------
# Independent validation gate (applies cleanly, but breaks the file)
# ---------------------------------------------------------------------------

# Applies cleanly against ORIGINAL_PY below, but the result is not valid Python
# (the `def` line loses its colon), so the validation gate must fail the run.
BREAKS_PYTHON_DIFF = """\
--- a/handler.py
+++ b/handler.py
@@ -1,2 +1,2 @@
-def handler():
+def handler(:
     return 1
"""

BREAKS_PYTHON_PROPOSAL_TEXT = f"""\
## Root Cause
Needs an argument.

## Proposed Patch
```diff
{BREAKS_PYTHON_DIFF}```

## Reasoning
Add a parameter.
"""

ORIGINAL_PY = "def handler():\n    return 1\n"


@pytest.mark.asyncio
async def test_open_pr_fails_closed_on_invalid_patched_python():
    """A diff that applies cleanly but leaves the file unparseable is caught by
    the validation gate BEFORE any branch/commit — the run fails closed."""
    mock_http = _make_mock_http(file_content=ORIGINAL_PY)
    client = GitHubClient(token="gh-token", client=mock_http)

    with pytest.raises(PatchValidationError, match="handler.py"):
        await client.open_pr(
            _make_alert(), _make_proposal(BREAKS_PYTHON_PROPOSAL_TEXT), repo="owner/repo",
        )

    # Fetched the file (GET) but never created a branch/commit/PR.
    mock_http.put.assert_not_called()
    post_urls = [c.args[0] for c in mock_http.post.call_args_list]
    assert post_urls == []

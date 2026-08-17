"""Apply patch proposals to a GitHub repo and open draft PRs via the Contents API."""

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from src.agent.alert_handler import ParsedAlert
from src.agent.patch_generator import PatchProposal
from src.agent.patch_validator import validate_patched_files
from src.config import config

log = logging.getLogger("llopster.github")


@dataclass
class PullRequest:
    number: int
    url: str
    branch: str


class PatchApplyError(Exception):
    """A hunk's context/removal lines do not match the target file.

    Fail-closed signal: the diff's line numbers or context don't line up with
    the real file, so applying it would corrupt the file. The caller must abort
    (no branch, no commit, no PR) rather than write a wrong change. This is a
    correctness failure, distinct from the benign "no parseable diff" case.
    """


class PatchValidationError(PatchApplyError):
    """The patched content of a touched file fails an independent validity check.

    A diff can apply cleanly and still leave a file that no longer parses
    (broken Python syntax, malformed YAML/JSON). This is the model-independent
    gate the confidence score cannot provide — a failure aborts the PR with the
    same fail-closed contract as PatchApplyError (no branch, no commit; the
    processor records the run ``failed``).
    """


class ProtectedPathError(PatchApplyError):
    """A patch targets a path the agent is not allowed to modify.

    Two independent gates raise this, both BEFORE any file is fetched or any
    branch/commit is created (fail closed, same abort contract as
    PatchApplyError — the processor records the run ``failed``):

      * A hard-deny of execution/deploy surfaces (``.github/`` workflows, CI
        configs, ``Dockerfile``, Helm chart templates). A diff against one of
        these runs in the repo's own CI context (with its secrets) the moment
        the PR opens, so a poisoned log line steering a context-matching diff
        at a workflow would be a privilege-escalation vector. This gate is
        unconditional.
      * An allowlist tying patched paths to the validated ``affected_files``
        set the investigation stage produced. When that set is available, any
        hunk targeting a file outside it is refused.
    """


# Execution/deploy surfaces an LLM-authored patch must NEVER touch. App-level
# config (e.g. an app's own values/`*.yaml`) is intentionally absent — those are
# legitimate patch targets (one seeded scenario fixes `helm-values.yaml`); the
# affected_files allowlist is the second gate for everything else.
_PROTECTED_DIR_COMPONENTS = {
    ".github",          # workflows + composite actions (run with repo secrets)
    ".circleci",
    ".buildkite",
    "helm-chart",       # chart templates define what the cluster runs
    "charts",
}
_PROTECTED_BASENAMES = {
    ".gitlab-ci.yml",
    ".travis.yml",
    "azure-pipelines.yml",
    "jenkinsfile",
    ".drone.yml",
    "bitbucket-pipelines.yml",
    "cloudbuild.yaml",
    ".woodpecker.yml",
}


def _normalize_repo_path(path: str) -> str:
    """Canonicalize a repo-relative path for comparison.

    Splits on `/` and drops empty and `.` segments (so `./a/b` → `a/b`) while
    preserving leading-dot filenames like `.github` and `..` traversal markers.
    """
    return "/".join(p for p in path.strip().split("/") if p and p != ".")


def _is_protected_path(path: str) -> bool:
    """True if `path` is an execution/deploy surface off-limits to patches."""
    norm = _normalize_repo_path(path)
    if not norm:
        return True  # empty/degenerate path — refuse
    parts = norm.split("/")
    if ".." in parts:
        return True  # path traversal — refuse
    lowered = [p.lower() for p in parts]
    if any(c in lowered for c in _PROTECTED_DIR_COMPONENTS):
        return True
    base = lowered[-1]
    if base in _PROTECTED_BASENAMES:
        return True
    if base.startswith("dockerfile") or base == "containerfile":
        return True
    if base.startswith("docker-compose"):
        return True
    return False


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

def _extract_diff(proposal_text: str) -> str:
    """Pull the raw unified diff out of the LLM response, stripping fences."""
    m = re.search(r"```(?:diff)?\s*\n(.*?)```", proposal_text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _parse_file_patches(diff: str) -> dict[str, list[tuple[int, list[str]]]]:
    """Parse a unified diff into {file_path: [(old_start, hunk_lines), ...]}.

    Each hunk_lines entry is a list of raw diff lines (space / minus / plus prefix).
    Returns an empty dict when the diff is empty or unparseable.
    """
    patches: dict[str, list[tuple[int, list[str]]]] = {}
    current_file: str | None = None
    current_hunks: list[tuple[int, list[str]]] = []
    current_hunk_lines: list[str] = []
    current_old_start = 0

    for line in diff.splitlines():
        if line.startswith("--- "):
            # Flush previous file
            if current_hunk_lines:
                current_hunks.append((current_old_start, current_hunk_lines))
                current_hunk_lines = []
            if current_file and current_hunks:
                patches[current_file] = current_hunks
            current_file = None
            current_hunks = []
        elif line.startswith("+++ "):
            path = line[4:].strip()
            current_file = path[2:] if path.startswith("b/") else path
        elif line.startswith("@@ "):
            if current_hunk_lines:
                current_hunks.append((current_old_start, current_hunk_lines))
                current_hunk_lines = []
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
            current_old_start = int(m.group(1)) if m else 1
        elif current_file and line and line[0] in (" ", "-", "+"):
            current_hunk_lines.append(line)

    # Flush last hunk and file
    if current_hunk_lines:
        current_hunks.append((current_old_start, current_hunk_lines))
    if current_file and current_hunks:
        patches[current_file] = current_hunks

    return patches


def _locate_hunk(
    lines: list[str], expected: list[str], header_idx: int, min_idx: int
) -> int:
    """Find the 0-indexed offset (>= min_idx) where a hunk should be applied.

    `expected` is the hunk's anchor: its context (" ") + removal ("-") content
    lines, in order — the lines that must already exist in the file. Behavior:

      * Exact header match → return the header offset (the common case; byte-
        for-byte the old behavior, so a correct diff applies unchanged).
      * Header doesn't match, but the anchor matches uniquely elsewhere → relocate
        to it. This is the drift fallback: a stale `@@ -N @@` header (the file
        shifted since the model read it) no longer fails to zero PRs when the
        surrounding code is still there and unambiguous.
      * Anchor matches in several places → pick the offset nearest the header
        (the diff's line number disambiguates repeats — patch(1)'s fuzz rule).
      * Anchor matches nowhere → raise PatchApplyError (fail closed, as before —
        a hallucinated context/removal line has no home in the real file).

    A pure-insertion hunk (no anchor lines at all) can't be searched by content,
    so its header offset is used with a strict bounds check.
    """
    def matches_at(idx: int) -> bool:
        if idx < min_idx or idx < 0 or idx + len(expected) > len(lines):
            return False
        return all(
            lines[idx + k].rstrip("\r\n") == exp.rstrip("\r\n")
            for k, exp in enumerate(expected)
        )

    if not expected:
        # Insertion-only hunk: nothing to anchor against; trust the header but
        # keep it in-bounds and after already-consumed lines.
        if header_idx < min_idx or header_idx > len(lines):
            raise PatchApplyError(
                f"hunk targets line {header_idx + 1}, outside the applicable "
                f"range of the file ({len(lines)} lines)"
            )
        return header_idx

    if matches_at(header_idx):
        return header_idx

    candidates = [
        j for j in range(min_idx, len(lines) - len(expected) + 1) if matches_at(j)
    ]
    if not candidates:
        found = (
            lines[header_idx].rstrip("\r\n")
            if 0 <= header_idx < len(lines) else "<end of file>"
        )
        raise PatchApplyError(
            f"context mismatch near line {header_idx + 1}: diff expected "
            f"{expected[0].rstrip(chr(13) + chr(10))!r}, file has {found!r} "
            f"(no relocated match found either)"
        )
    best = min(candidates, key=lambda j: (abs(j - header_idx), j))
    if best != header_idx:
        log.info(
            "relocated drifted hunk: header line %d → line %d",
            header_idx + 1, best + 1,
        )
    return best


def _apply_patch(original: str, hunks: list[tuple[int, list[str]]]) -> str:
    """Apply a list of (old_start, hunk_lines) to the original file text.

    old_start is 1-indexed (as in the unified diff header). Each hunk is located
    by its anchor (context + removal content) via `_locate_hunk`, which prefers
    the header line but relocates a drifted-but-matching hunk instead of failing
    to zero PRs. A hunk whose anchor matches nowhere in the file — a hallucinated
    context/removal line — still raises PatchApplyError (fail closed): applying
    it would corrupt the file, so the caller aborts (no branch, no commit).
    """
    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    result: list[str] = []
    src_idx = 0  # 0-indexed cursor into original lines

    for old_start, hunk_lines in hunks:
        expected = [line[1:] for line in hunk_lines if line and line[0] in (" ", "-")]
        target = _locate_hunk(lines, expected, old_start - 1, src_idx)
        result.extend(lines[src_idx:target])
        src_idx = target

        for line in hunk_lines:
            prefix, content = line[0], line[1:]
            if prefix in (" ", "-"):
                # _locate_hunk guaranteed these anchor lines match at src_idx.
                if prefix == " ":
                    result.append(lines[src_idx])
                src_idx += 1
            elif prefix == "+":
                if not content.endswith("\n"):
                    content += "\n"
                result.append(content)

    result.extend(lines[src_idx:])
    return "".join(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_branch_name(alert: ParsedAlert) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9-]", "-", alert.alertname).lower().strip("-")
    return f"llopster/{slug}-{ts}"


def _extract_section(text: str, heading: str) -> str:
    m = re.search(
        rf"##\s+{re.escape(heading)}\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL
    )
    return m.group(1).strip() if m else ""


def _pr_body(alert: ParsedAlert, proposal: PatchProposal) -> str:
    root_cause = _extract_section(proposal.text, "Root Cause")
    reasoning = _extract_section(proposal.text, "Reasoning")
    return "\n".join([
        f"**Alert:** `{alert.alertname}` · severity: `{alert.severity}` · service: `{alert.service}`",
        "",
        "## Root Cause",
        root_cause or "_Not available._",
        "",
        "## Reasoning",
        reasoning or "_Not available._",
        "",
        "---",
        f"_Generated by llopster · model: `{proposal.model}` · "
        f"{proposal.input_tokens:,} input tokens · {proposal.output_tokens:,} output tokens_",
    ])


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def proposal_has_patch(proposal_text: str) -> bool:
    """Return True if the proposal contains at least one parseable file patch."""
    return bool(_parse_file_patches(_extract_diff(proposal_text)))


class GitHubClient:
    def __init__(
        self, token: str, client: httpx.AsyncClient, *, api_base: str | None = None
    ):
        self._client = client
        # REST API root. Defaults to the public github.com API (config's own
        # default), so github.com installs are unchanged. GitHub Enterprise
        # Server serves the identical v3 API under https://<host>/api/v3 —
        # every request below is built from this root, so pointing it at a
        # GHES instance is the whole of the change.
        self._api_base = (api_base or config.github_api_base).rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _base(self, repo: str) -> str:
        return f"{self._api_base}/repos/{repo}"

    async def open_pr(
        self, alert: ParsedAlert, proposal: PatchProposal, repo: str,
        *, draft: bool = True, allowed_paths: set[str] | None = None,
    ) -> PullRequest:
        diff = _extract_diff(proposal.text)
        file_patches = _parse_file_patches(diff)
        if not file_patches:
            raise ValueError("no parseable file patches in proposal — skipping PR")

        # Path gates — BEFORE any network call. Refuse the whole PR (fail
        # closed) if any hunk targets a protected execution/deploy surface, or
        # (when the investigation supplied a validated allowlist) a file outside
        # it. Nothing is fetched, no branch/commit is created.
        allowed_norm = (
            {_normalize_repo_path(p) for p in allowed_paths}
            if allowed_paths is not None else None
        )
        for file_path in file_patches:
            if _is_protected_path(file_path):
                raise ProtectedPathError(
                    f"{file_path}: refusing to patch a protected execution/deploy "
                    f"surface (.github/CI/Dockerfile/chart template)"
                )
            if allowed_norm is not None and _normalize_repo_path(file_path) not in allowed_norm:
                raise ProtectedPathError(
                    f"{file_path}: not in the investigation's affected_files "
                    f"allowlist ({sorted(allowed_norm)})"
                )

        default_branch, base_sha = await self._get_default_branch(repo)

        # Fetch + apply every file BEFORE creating the branch or committing
        # anything. A context mismatch on any hunk raises PatchApplyError here,
        # aborting the whole PR (fail closed) without leaving a half-patched
        # branch behind — nothing is written to the repo unless every hunk
        # verifies against the real file.
        patched_files: list[tuple[str, str, str]] = []  # (path, content, file_sha)
        for file_path, hunks in file_patches.items():
            original, file_sha = await self._get_file(repo, file_path, default_branch)
            try:
                patched = _apply_patch(original, hunks)
            except PatchApplyError as e:
                raise PatchApplyError(f"{file_path}: {e}") from e
            patched_files.append((file_path, patched, file_sha))

        # Independent validation gate — run the patched content of every touched
        # file through a model-independent parse/compile check BEFORE creating
        # the branch. A clean-applying diff that leaves a file unparseable fails
        # the run closed here rather than opening a broken PR.
        validation = validate_patched_files(
            [(path, content) for path, content, _ in patched_files]
        )
        if not validation.ok:
            raise PatchValidationError("; ".join(validation.errors))

        branch = _make_branch_name(alert)
        await self._create_branch(repo, branch, base_sha)
        log.info("created branch %s on %s", branch, repo)

        for file_path, patched, file_sha in patched_files:
            await self._update_file(
                repo, file_path, patched, file_sha, branch,
                f"fix({alert.service}): {alert.alertname} — applied llopster patch",
            )
            log.info("patched %s on branch %s (%s)", file_path, branch, repo)

        pr = await self._create_pr(repo, alert, proposal, branch, default_branch, draft=draft)
        log.info("opened %sPR #%d: %s", "draft " if draft else "", pr.number, pr.url)
        return pr

    async def _get_default_branch(self, repo: str) -> tuple[str, str]:
        base = self._base(repo)
        resp = await self._client.get(base, headers=self._headers)
        resp.raise_for_status()
        branch_name = resp.json()["default_branch"]

        ref_resp = await self._client.get(
            f"{base}/git/ref/heads/{branch_name}", headers=self._headers,
        )
        ref_resp.raise_for_status()
        sha = ref_resp.json()["object"]["sha"]
        return branch_name, sha

    async def _create_branch(self, repo: str, branch: str, sha: str) -> None:
        resp = await self._client.post(
            f"{self._base(repo)}/git/refs",
            headers=self._headers,
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        resp.raise_for_status()

    async def _get_file(self, repo: str, path: str, ref: str) -> tuple[str, str]:
        resp = await self._client.get(
            f"{self._base(repo)}/contents/{path}",
            headers=self._headers,
            params={"ref": ref},
        )
        resp.raise_for_status()
        data = resp.json()
        content = base64.b64decode(data["content"]).decode()
        return content, data["sha"]

    async def _update_file(
        self, repo: str, path: str, content: str, file_sha: str, branch: str, message: str
    ) -> None:
        resp = await self._client.put(
            f"{self._base(repo)}/contents/{path}",
            headers=self._headers,
            json={
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "sha": file_sha,
                "branch": branch,
            },
        )
        resp.raise_for_status()

    async def _create_pr(
        self,
        repo: str,
        alert: ParsedAlert,
        proposal: PatchProposal,
        branch: str,
        base: str,
        *,
        draft: bool = True,
    ) -> PullRequest:
        resp = await self._client.post(
            f"{self._base(repo)}/pulls",
            headers=self._headers,
            json={
                "title": f"fix({alert.service}): {alert.alertname}",
                "body": _pr_body(alert, proposal),
                "head": branch,
                "base": base,
                "draft": draft,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return PullRequest(number=data["number"], url=data["html_url"], branch=branch)

    async def get_pr_status(self, repo: str, pr_number: int) -> str:
        """Return 'open', 'closed', or 'merged'."""
        resp = await self._client.get(
            f"{self._base(repo)}/pulls/{pr_number}",
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("merged"):
            return "merged"
        return data.get("state", "unknown")

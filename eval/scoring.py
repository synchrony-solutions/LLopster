"""Score a replayed Run against a scenario's ground truth.

The grade is intentionally simple and explainable — an acquirer's diligence
team should be able to read exactly why a scenario passed or failed:

  correct — the agent proposed an actionable patch AND it targets the
            ground-truth file(s) (the bug really is in `check_db_pool.py`).
  partial — it got *one* of the two: proposed a patch but touched the wrong
            file, or located the right file (investigation) but produced no
            patch. Real signal, not a full pass.
  wrong   — no actionable patch and the right file was never located, or the
            run failed.

`expect_patch: false` scenarios invert this (the right answer is to NOT patch),
so the harness can also score noise-suppression once such scenarios exist.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from eval.corpus import Scenario
from src.db.models import Run


@dataclass
class ScenarioScore:
    scenario_id: str
    label: str  # one of EVAL_LABELS
    reason: str
    run_id: str | None
    status: str | None
    patch_proposed: bool
    targets_expected_file: bool
    root_cause_match: bool
    expected_files: list[str] = field(default_factory=list)
    confidence: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CorpusScore:
    scenario_count: int
    correct_count: int
    partial_count: int
    wrong_count: int
    scores: list[ScenarioScore]

    @property
    def pass_rate(self) -> float:
        return (self.correct_count / self.scenario_count) if self.scenario_count else 0.0


def _basename(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name


def _file_mentioned(expected: str, haystack: str) -> bool:
    """True if the expected file's basename appears in a blob (diff text)."""
    return _basename(expected).lower() in (haystack or "").lower()


def _targets_expected_file(run: Run, expected_files: tuple[str, ...]) -> bool:
    """Did the agent point at a ground-truth file?

    Counts a hit in either the proposed diff (the synthesis touched it) or the
    investigation's affected-files list (Sonnet located it). Empty
    `expected_files` means we don't assert a target — treat as satisfied.
    """
    if not expected_files:
        return True
    diff = run.parsed_diff or ""
    investigated = {
        _basename(p).lower()
        for p in (run.investigation_affected_files_json or [])
    }
    for expected in expected_files:
        base = _basename(expected).lower()
        if base in investigated or _file_mentioned(expected, diff):
            return True
    return False


def _patch_proposed(run: Run) -> bool:
    return bool(run.parsed_diff and run.parsed_diff.strip())


def _root_cause_match(run: Run, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return False
    blob = f"{run.parsed_root_cause or ''}\n{run.llm_response_text or ''}".lower()
    return any(kw.lower() in blob for kw in keywords)


def score_run(scenario: Scenario, run: Run | None) -> ScenarioScore:
    """Grade one completed Run against its scenario."""
    gt = scenario.ground_truth

    if run is None:
        return ScenarioScore(
            scenario_id=scenario.id,
            label="wrong",
            reason="no run row produced",
            run_id=None,
            status=None,
            patch_proposed=False,
            targets_expected_file=False,
            root_cause_match=False,
            expected_files=list(gt.expected_files),
        )

    patch_proposed = _patch_proposed(run)
    targets = _targets_expected_file(run, gt.expected_files)
    rc_match = _root_cause_match(run, gt.root_cause_keywords)

    if run.processing_status == "failed":
        label, reason = "wrong", f"run failed: {run.error_message or 'unknown error'}"
    elif gt.expect_patch:
        if patch_proposed and targets:
            label = "correct"
            reason = "patch proposed and targets the ground-truth file"
        elif patch_proposed or targets:
            label = "partial"
            reason = (
                "patch proposed but wrong file"
                if patch_proposed
                else "located the file but produced no patch"
            )
        else:
            label = "wrong"
            reason = f"no actionable patch (status={run.processing_status})"
    else:
        # Noise-suppression scenario: the right answer is NOT to patch.
        if patch_proposed:
            label, reason = "wrong", "patched a scenario that should have been skipped"
        else:
            label, reason = "correct", "correctly produced no patch"

    return ScenarioScore(
        scenario_id=scenario.id,
        label=label,
        reason=reason,
        run_id=run.id,
        status=run.processing_status,
        patch_proposed=patch_proposed,
        targets_expected_file=targets,
        root_cause_match=rc_match,
        expected_files=list(gt.expected_files),
        confidence=run.parsed_confidence,
    )


def aggregate(scores: list[ScenarioScore]) -> CorpusScore:
    correct = sum(1 for s in scores if s.label == "correct")
    partial = sum(1 for s in scores if s.label == "partial")
    wrong = sum(1 for s in scores if s.label == "wrong")
    return CorpusScore(
        scenario_count=len(scores),
        correct_count=correct,
        partial_count=partial,
        wrong_count=wrong,
        scores=scores,
    )

"""SQLAlchemy models for run history and settings.

Schema design notes:
  - `Run` is the single source of truth for "what did the agent do for this
    alert" — the read-only UI queries this table and nothing else.
  - JSON columns (`alert_payload_json`, `log_lines_json`, ...) keep the table
    flat for now; we can normalize into child tables later if access patterns
    demand it.
  - `processing_status` advances pending → collecting → generating → posting →
    done (or skipped / failed). The UI polls this field for live updates.
  - `triggered_by_user_id` is nullable today; populated once auth lands.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# Allowed operator ground-truth labels for a Run. These are the human verdict
# on whether the agent's proposed fix was right — the data-collection half of
# the eval / ground-truth flywheel (ROADMAP Track B). Kept here so the write
# helper, the API layer, and the UI all validate against one source of truth.
OPERATOR_LABELS = ("correct", "wrong", "partial", "na")


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    # Identity
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    fingerprint: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    # Alert
    alertname: Mapped[str] = mapped_column(String(256), index=True)
    service: Mapped[str | None] = mapped_column(String(256), index=True, nullable=True)
    severity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="firing")
    alert_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Context collection
    logql_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    promql_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    lookback_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_line_count: Mapped[int] = mapped_column(Integer, default=0)
    metric_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    collection_errors_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    log_lines_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    metric_samples_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    # Triage stage (Haiku gate — runs before context collection so noise
    # alerts cost ~$0.001 instead of a full synthesis call). All NULL on
    # runs that pre-date the triage gate or that bypassed it (e.g. severity
    # = "critical", which we hard-pass without spending a Haiku call).
    triage_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "proceed" | "skip"
    triage_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triage_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    triage_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triage_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triage_cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triage_cache_creation_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Investigation stage (Sonnet — runs after Loki/Prom collection; in
    # Phase B these columns are populated but Opus still sees the full
    # codebase blob.  Phase C will use `investigation_affected_files_json`
    # to slice Opus's input).  All NULL on runs that pre-date the
    # investigation step or that bypassed it (fail-open path, disabled
    # setting, triage-skip path).
    investigation_root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    investigation_affected_files_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    investigation_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    investigation_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    investigation_response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    investigation_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    investigation_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    investigation_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    investigation_cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    investigation_cache_creation_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    investigation_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # LLM call (synthesis stage — the existing Opus patch call)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_creation_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_confidence_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase C: True when Opus saw only the files Sonnet flagged in
    # `investigation_affected_files_json`. False when the empty-blob
    # safety net (or absent / empty investigation) flipped us back to
    # the full codebase. NULL on runs that pre-date Phase C.
    synthesis_used_narrowed_context: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Outcomes
    pr_opened: Mapped[bool] = mapped_column(Boolean, default=False)
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # open/closed/merged
    pr_merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slack_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    slack_skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Operator ground-truth label (eval flywheel). NULL until an operator
    # judges the run from the dashboard's run-detail page. `operator_label` is
    # one of OPERATOR_LABELS; the note is free-text rationale; labeled_by is
    # nullable today and populated once auth lands (mirrors
    # triggered_by_user_id). Indexed so the eval harness can pull labeled runs
    # cheaply.
    operator_label: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    operator_label_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operator_labeled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Provenance
    trigger_source: Mapped[str] = mapped_column(String(32), default="alertmanager")
    triggered_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Lifecycle
    processing_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Setting(Base):
    """Key-value settings store. Phase D will populate this from the UI; for
    now it exists so the schema is in place and we don't need a migration to
    enable it later."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# Outcome labels the eval scorer assigns to a replayed scenario. Intentionally
# the same vocabulary an operator uses on a real Run (`OPERATOR_LABELS`) so the
# harness pass-rate and the human-labeled pass-rate read on one scale:
#   correct — proposed a patch that targets the ground-truth file(s)
#   partial — proposed a patch OR located the file, but not both
#   wrong   — no actionable patch (or a failure) where one was expected
EVAL_LABELS = ("correct", "partial", "wrong")


class EvalRun(Base):
    """One execution of the frozen scenario corpus through the pipeline.

    This is the persistence half of the eval / ground-truth flywheel
    (ROADMAP Track B): each row is a timestamped snapshot of how the agent
    scored against the regression baseline, so the dashboard can render a
    *trend* (the asset grows row-by-row) rather than a single point-in-time
    number. `results_json` carries the per-scenario breakdown so a regression
    can be traced to the scenario that flipped.
    """

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    # Provenance of this eval run.
    corpus_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)  # synthesis model, or "mock"
    trigger_source: Mapped[str] = mapped_column(String(32), default="cli")

    # Aggregate score.
    scenario_count: Mapped[int] = mapped_column(Integer, default=0)
    correct_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    wrong_count: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[float] = mapped_column(Float, default=0.0)  # correct / scenario_count

    # Per-scenario breakdown: [{scenario, label, reason, expected_files, ...}].
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

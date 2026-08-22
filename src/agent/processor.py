"""Per-alert processing pipeline.

`process_alert` is the single entry point used by both the AlertManager
webhook and (later) the manual-trigger UI. It owns the lifecycle of a Run
row, advancing `processing_status` through pending → collecting → generating
→ posting → done (or skipped/failed) and persisting context, LLM output,
and outcome decisions at each phase boundary.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from src.agent.alert_filter import parse_extra_ignore_setting, should_skip
from src.agent.alert_handler import ParsedAlert
from src.agent.context_collector import ContextCollector
from src.agent.cost_breaker import check_cost_breaker
from src.agent.dedup import (
    DEFAULT_POST_MERGE_GRACE_MINUTES,
    PreviousAttempt,
    compute_dedup_key,
    find_blocking_open_pr_run,
    find_previous_merged_attempt,
    find_recent_unproductive_run,
    is_within_grace_window,
    previous_attempt_from_run,
)
from src.agent.investigator import Investigator
from src.agent.patch_generator import PatchGenerator
from src.agent.processing_mode import MANUAL, set_processing_mode
from src.agent.triage import Triage
from src.config import config
from src.db import repository as repo
from src.integrations.github_client import (
    GitHubClient,
    PatchApplyError,
    _extract_diff,
    proposal_has_patch,
)
from src.integrations.notifier import Notifier
from src.services_registry import ServiceRegistry

# Severities that bypass the Haiku triage gate entirely. Critical alerts
# go straight to investigation — no token cost on the highest-stakes path
# and no failure mode where Haiku mis-skips a P1.
_TRIAGE_BYPASS_SEVERITIES = {"critical"}

# Boolean values accepted from the `triage_enabled` setting key.
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

log = logging.getLogger("llopster.processor")


def _parse_root_cause(text: str) -> str | None:
    m = re.search(r"##\s+Root Cause\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else None


async def process_alert(
    run_id: str,
    alert: ParsedAlert,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    collector: ContextCollector,
    services: ServiceRegistry,
    patcher: PatchGenerator | None,
    github: GitHubClient | None,
    notifier: Notifier | None,
    triage: Triage | None = None,
    investigator: Investigator | None = None,
    lookback_minutes: int | None = None,
    enforce_cost_breaker: bool = True,
) -> None:
    """Run the full pipeline against a previously-created Run row.

    All exceptions are caught and recorded on the row as `failed` — this
    function never raises (it's invoked from a fire-and-forget background
    task).
    """
    try:
        # ---- Read runtime settings (DB overrides env config) ------------
        async with sessionmaker() as session:
            confidence_threshold = int(
                (await repo.get_setting(session, "patch_confidence_threshold"))
                or config.patch_confidence_threshold
            )
            db_lookback = await repo.get_setting(session, "log_lookback_minutes")
            if lookback_minutes is None and db_lookback is not None:
                lookback_minutes = int(db_lookback)
            extra_ignore = parse_extra_ignore_setting(
                await repo.get_setting(session, "ignore_alertnames")
            )
            triage_enabled_raw = (
                await repo.get_setting(session, "triage_enabled")
            )
            triage_min_confidence_raw = (
                await repo.get_setting(session, "triage_min_confidence")
            )
            investigation_enabled_raw = (
                await repo.get_setting(session, "investigation_enabled")
            )
            open_prs_as_draft_raw = (
                await repo.get_setting(session, "open_prs_as_draft")
            )
        triage_enabled = (
            config.triage_enabled
            if triage_enabled_raw is None
            else triage_enabled_raw.strip().lower() in _TRUE_VALUES
        )
        triage_min_confidence = (
            int(triage_min_confidence_raw)
            if triage_min_confidence_raw
            else config.triage_min_confidence
        )
        investigation_enabled = (
            config.investigation_enabled
            if investigation_enabled_raw is None
            else investigation_enabled_raw.strip().lower() in _TRUE_VALUES
        )
        open_prs_as_draft = (
            config.open_prs_as_draft
            if open_prs_as_draft_raw is None
            else open_prs_as_draft_raw.strip().lower() in _TRUE_VALUES
        )

        # ---- Pre-pipeline filter (cheap rejection BEFORE Loki/Prom) -----
        # Saves a Loki query, a Prometheus query, and an LLM call for any
        # alert that no patch could ever fix. Status moves directly to
        # `skipped` with a reason; the Run row stays for dashboard
        # visibility but no outbound HTTP fires.
        decision = should_skip(alert, services=services, extra_ignore_alertnames=extra_ignore)
        if decision.skip:
            log.info("[%s] pre-filter skip: %s", run_id, decision.reason)
            async with sessionmaker() as session:
                await repo.update_status(session, run_id, "skipped", error=decision.reason)
            return

        # ---- Cost circuit breaker (BEFORE any LLM call) -----------------
        # If a runs/hour or USD/day ceiling has been reached, trip the agent
        # into manual mode so new alerts park at `queued`, and short-circuit
        # this run before spending a single token. Skipped for operator-
        # initiated runs (manual trigger / dispatch) — once tripped, the
        # operator must still be able to drain the queue by hand. Fails safe:
        # a breaker error returns "not tripped" and the pipeline proceeds.
        if enforce_cost_breaker:
            breaker = await check_cost_breaker(sessionmaker)
            if breaker.tripped:
                log.warning("[%s] %s — tripping to manual mode", run_id, breaker.reason)
                async with sessionmaker() as session:
                    await set_processing_mode(session, MANUAL)
                    await repo.update_status(session, run_id, "queued", error=breaker.reason)
                return

        # ---- Dedup: suppress if a prior Run for the same alert already
        # has an open PR. Prevents re-running the full pipeline (and burning
        # Opus tokens) on every re-firing while the fix is in review.
        dedup_key = compute_dedup_key(alert)
        async with sessionmaker() as session:
            blocker = await find_blocking_open_pr_run(
                session, dedup_key, exclude_run_id=run_id,
            )
            backoff_setting = await repo.get_setting(session, "patch_backoff_minutes")
        if blocker is not None:
            reason = f"duplicate-pending-pr: run {blocker.id} (PR {blocker.pr_url})"
            log.info("[%s] %s", run_id, reason)
            async with sessionmaker() as session:
                await repo.update_status(session, run_id, "skipped", error=reason)
            return

        # ---- Post-firing backoff: close the "re-fires forever, never opens a
        # PR" cost loop. A below-confidence-threshold or no-patch alert never
        # opens a PR, so the open-PR dedup above never matches it; without this,
        # every re-firing re-runs the full Haiku→Sonnet→Opus pipeline at full
        # cost. If a real pipeline run for this alert finished WITHOUT a PR
        # inside the backoff window, suppress this firing before any LLM call
        # (one re-investigation per window). 0 disables it. Operator-initiated
        # runs skip the breaker; the backoff is a firing-driven cost guard, so
        # it stays on for both webhook and dispatch — a queued run being drained
        # by hand won't match (its own row is excluded, and it opens a PR or is
        # the fresh anchor).
        backoff_minutes = (
            int(backoff_setting) if backoff_setting else config.patch_backoff_minutes
        )
        if backoff_minutes > 0:
            since = datetime.now(timezone.utc) - timedelta(minutes=backoff_minutes)
            async with sessionmaker() as session:
                recent = await find_recent_unproductive_run(
                    session, dedup_key, since=since, exclude_run_id=run_id,
                )
            if recent is not None:
                reason = (
                    f"backoff: run {recent.id} ({recent.processing_status}) finished "
                    f"without a PR within {backoff_minutes}m — suppressing re-run"
                )
                log.info("[%s] %s", run_id, reason)
                async with sessionmaker() as session:
                    await repo.update_status(session, run_id, "skipped", error=reason)
                return

        # ---- Post-deploy re-evaluation: if the most recent fix for this
        # alert has been merged, decide based on the grace window.
        #   * Within grace → the deploy hasn't rolled out yet; suppress.
        #   * Past grace   → the fix is live and the alert is still firing,
        #                     so the prior diagnosis or fix was wrong.
        #                     Proceed, but attach the prior attempt so the
        #                     model doesn't re-propose the same losing diff.
        async with sessionmaker() as session:
            grace_setting = await repo.get_setting(session, "post_merge_grace_minutes")
            prev_run = await find_previous_merged_attempt(
                session, dedup_key, exclude_run_id=run_id,
            )
        grace_minutes = (
            int(grace_setting) if grace_setting else DEFAULT_POST_MERGE_GRACE_MINUTES
        )

        previous_attempt: PreviousAttempt | None = None
        if prev_run is not None and prev_run.pr_merged_at is not None:
            if is_within_grace_window(prev_run.pr_merged_at, grace_minutes):
                reason = (
                    f"within-deploy-grace: PR {prev_run.pr_url} merged at "
                    f"{prev_run.pr_merged_at.isoformat()} "
                    f"(grace={grace_minutes}m)"
                )
                log.info("[%s] %s", run_id, reason)
                async with sessionmaker() as session:
                    await repo.update_status(session, run_id, "skipped", error=reason)
                return
            previous_attempt = previous_attempt_from_run(prev_run)
            log.info(
                "[%s] previous fix attempt detected (PR %s); attaching as context",
                run_id, prev_run.pr_url,
            )

        if patcher is None:
            log.info("[%s] patch generation disabled (no API key); skipping", run_id)
            async with sessionmaker() as session:
                await repo.update_status(
                    session, run_id, "skipped",
                    error="ANTHROPIC_API_KEY not set — patch generation disabled",
                )
            return

        service_cfg = services.get(alert.service)  # safe: should_skip already verified
        # Per-service premium-pack selector (None = Community prompts). Passed
        # into every LLM stage so prompt overlays resolve for this service's
        # technology stack; absent field → no overlay, baked-in prompt used.
        stack = service_cfg.pack if service_cfg is not None else None

        # Captured from the triage step (if it ran) and forwarded into
        # investigation so Sonnet builds on Haiku's framing rather than
        # rediscovering it.
        triage_reasoning_for_investigation: str | None = None

        # ---- Triage gate (Haiku) ----------------------------------------
        # Cheap pre-flight classification BEFORE we spend a Loki query, a
        # Prometheus query, and an Opus call. Designed to be additive and
        # reversible: misclassifications still surface on the dashboard
        # (the Run row keeps a triage_reasoning column), and the gate has
        # two kill switches — global `triage_enabled` and the per-run
        # `triage_min_confidence` fallthrough that lets borderline Haiku
        # decisions pass through to the full pipeline.
        if (
            triage is not None
            and triage_enabled
            and (alert.severity or "").lower() not in _TRIAGE_BYPASS_SEVERITIES
        ):
            async with sessionmaker() as session:
                await repo.update_status(session, run_id, "triaging")
            try:
                decision = await triage.evaluate(
                    alert, service_known=service_cfg is not None, stack=stack,
                )
            except Exception as e:
                # Fail-open: a Haiku outage must not block real incidents.
                log.warning(
                    "[%s] triage call failed (%s); falling through to full pipeline",
                    run_id, e,
                )
            else:
                async with sessionmaker() as session:
                    await repo.record_triage(session, run_id, decision)
                if (
                    not decision.proceed
                    and decision.confidence >= triage_min_confidence
                ):
                    reason = (
                        f"triage-skip ({decision.confidence}/5): "
                        f"{decision.reasoning}"
                    )
                    log.info("[%s] %s", run_id, reason)
                    async with sessionmaker() as session:
                        await repo.update_status(
                            session, run_id, "skipped", error=reason,
                        )
                    return
                log.info(
                    "[%s] triage decision=%s confidence=%d/5",
                    run_id, decision.decision_label, decision.confidence,
                )
                # Forward Haiku's reasoning into Sonnet so investigation
                # builds on it rather than starting cold.
                triage_reasoning_for_investigation = decision.reasoning

        # ---- Phase 1: collect context -----------------------------------
        async with sessionmaker() as session:
            await repo.update_status(session, run_id, "collecting")

        effective_lookback = lookback_minutes if lookback_minutes is not None else config.log_lookback_minutes
        active_collector = (
            ContextCollector(
                loki=collector.loki,
                prometheus=collector.prometheus,
                lookback_minutes=effective_lookback,
                max_log_lines=collector.max_log_lines,
                scope_labels=collector.scope_labels,
            )
            if lookback_minutes is not None
            else collector
        )
        ctx = await active_collector.collect(alert)

        async with sessionmaker() as session:
            await repo.record_collected_context(
                session, run_id, ctx, lookback_minutes=effective_lookback,
            )

        # ---- Investigation (Sonnet) -------------------------------------
        # Reads alert + logs + metrics + codebase OUTLINE (paths + line
        # counts only — not contents) and produces a root-cause
        # hypothesis plus a short list of likely-affected files. The
        # result is recorded on the Run row AND forwarded into Opus's
        # synthesis call (Phase C), where the patch generator uses
        # `investigation.affected_files` to narrow the codebase blob.
        # PatchGenerator owns the narrow-vs-full fallback ladder — the
        # processor's only job here is "run Sonnet and pass the result".
        #
        # Fail-open: any error (API outage, model rejecting params,
        # parser glitch) is logged and the run continues to synthesis
        # against the full codebase. We never want investigation issues
        # to block real incidents from reaching the patch step.
        investigation_for_synthesis = None
        if investigator is not None and investigation_enabled:
            async with sessionmaker() as session:
                await repo.update_status(session, run_id, "investigating")
            try:
                t_inv = time.monotonic()
                investigation = await investigator.investigate(
                    ctx,
                    codebase_path=service_cfg.codebase_path,
                    triage_reasoning=triage_reasoning_for_investigation,
                    stack=stack,
                    chart_lineage=service_cfg.chart_lineage,
                )
                inv_latency_ms = int((time.monotonic() - t_inv) * 1000)
            except Exception as e:
                log.warning(
                    "[%s] investigation call failed (%s); falling through to synthesis",
                    run_id, e,
                )
            else:
                async with sessionmaker() as session:
                    await repo.record_investigation(
                        session, run_id, investigation, inv_latency_ms,
                    )
                investigation_for_synthesis = investigation
                log.info(
                    "[%s] investigation: confidence=%d/5 affected_files=%d",
                    run_id, investigation.confidence,
                    len(investigation.affected_files),
                )

        # ---- Phase 2: generate patch ------------------------------------
        async with sessionmaker() as session:
            await repo.update_status(session, run_id, "generating")

        t0 = time.monotonic()
        proposal = await patcher.generate(
            ctx,
            codebase_path=service_cfg.codebase_path,
            previous_attempt=previous_attempt,
            investigation=investigation_for_synthesis,
            stack=stack,
            delivery=service_cfg.delivery,
            chart_lineage=service_cfg.chart_lineage,
            github_repo=service_cfg.github_repo,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "[%s] synthesis: narrowed=%s files=%d input=%d output=%d",
            run_id, proposal.used_narrowed_context,
            proposal.file_count, proposal.input_tokens, proposal.output_tokens,
        )

        async with sessionmaker() as session:
            await repo.record_llm_response(
                session,
                run_id,
                proposal,
                latency_ms=latency_ms,
                parsed_root_cause=_parse_root_cause(proposal.text),
                parsed_diff=_extract_diff(proposal.text),
            )

        # ---- Phase 3: post outcomes (PR + Slack) ------------------------
        async with sessionmaker() as session:
            await repo.update_status(session, run_id, "posting")

        pr_url: str | None = None
        pr_skip_reason: str | None = None
        if github is None:
            pr_skip_reason = "GITHUB_TOKEN not set"
        elif not proposal_has_patch(proposal.text):
            pr_skip_reason = "no actionable patch in response"
        elif proposal.confidence < confidence_threshold:
            pr_skip_reason = (
                f"confidence {proposal.confidence}/5 below threshold "
                f"{confidence_threshold}"
            )
        else:
            try:
                # When the investigation stage produced a validated list of
                # affected files, constrain the patch to it — a hunk targeting
                # anything else fails the run closed. None (investigation
                # disabled or empty) leaves only the unconditional hard-deny of
                # execution/deploy surfaces active inside open_pr.
                allowed_paths = (
                    set(investigation_for_synthesis.affected_files)
                    if investigation_for_synthesis
                    and investigation_for_synthesis.affected_files
                    else None
                )
                # When an indirect delivery mode points at a version reference
                # in THIS repo, the synthesis prompt requires the bump in the
                # same diff. The investigator never saw that file (it is a
                # deploy-side value, not a code path), so the allowlist would
                # refuse the very patch we asked for. Widen it by exactly that
                # one declared path — no wider.
                version_ref_path = _same_repo_version_ref_path(service_cfg)
                if allowed_paths is not None and version_ref_path:
                    allowed_paths = allowed_paths | {version_ref_path}
                pr = await github.open_pr(
                    alert, proposal, repo=service_cfg.github_repo,
                    draft=open_prs_as_draft, allowed_paths=allowed_paths,
                )
                pr_url = pr.url
                async with sessionmaker() as session:
                    await repo.record_pr(
                        session, run_id,
                        pr_url=pr.url, pr_number=pr.number, pr_branch=pr.branch,
                    )
            except PatchApplyError as e:
                # The diff didn't line up with the real file — applying it
                # would corrupt the file, so open_pr aborted before writing
                # anything. Fail the run loudly (correctness failure) rather
                # than recording it as a benign skip and reporting `done`.
                log.error("[%s] patch verification failed, no PR opened: %s", run_id, e)
                error_msg = f"patch verification failed: {e}"
                async with sessionmaker() as session:
                    await repo.record_pr(session, run_id, skip_reason=error_msg)
                    await repo.update_status(session, run_id, "failed", error=error_msg)
                return
            except Exception as e:
                log.exception("[%s] github PR creation failed: %s", run_id, e)
                pr_skip_reason = f"PR creation error: {e}"

        if pr_skip_reason is not None:
            async with sessionmaker() as session:
                await repo.record_pr(session, run_id, skip_reason=pr_skip_reason)

        # Notification (Slack / Teams / none). Stored on the same
        # slack_notified / slack_skip_reason columns regardless of provider.
        notify_skip_reason: str | None = None
        notified = False
        if notifier is None:
            notify_skip_reason = "notifications disabled (no notifier configured)"
        else:
            try:
                await notifier.post_patch(alert, proposal, pr_url=pr_url)
                notified = True
            except Exception as e:
                log.exception("[%s] %s notification failed: %s", run_id, notifier.provider, e)
                notify_skip_reason = f"{notifier.provider} post error: {e}"
        async with sessionmaker() as session:
            await repo.record_notification(
                session, run_id, notified=notified, skip_reason=notify_skip_reason,
            )

        # ---- Done -------------------------------------------------------
        async with sessionmaker() as session:
            await repo.update_status(session, run_id, "done")
        log.info(
            "[%s] complete: confidence=%d pr=%s notify=%s",
            run_id, proposal.confidence,
            pr_url or "skipped", "yes" if notified else "skipped",
        )

    except Exception as e:
        log.exception("[%s] processing failed: %s", run_id, e)
        try:
            async with sessionmaker() as session:
                await repo.update_status(session, run_id, "failed", error=str(e))
        except Exception:
            log.exception("[%s] also failed to record failure status", run_id)


def alert_to_payload(alert: ParsedAlert) -> dict[str, Any]:
    """Round-trip a ParsedAlert into a JSON-friendly dict for the
    `alert_payload_json` column. Used when creating a Run from a synthesized
    alert (no original webhook payload to store)."""
    return {
        "fingerprint": alert.fingerprint,
        "status": alert.status,
        "alertname": alert.alertname,
        "severity": alert.severity,
        "service": alert.service,
        "summary": alert.summary,
        "description": alert.description,
        "starts_at": alert.starts_at.isoformat() if alert.starts_at else None,
        "ends_at": alert.ends_at.isoformat() if alert.ends_at else None,
        "labels": alert.labels,
        "annotations": alert.annotations,
        "generator_url": alert.generator_url,
    }


def _same_repo_version_ref_path(service_cfg) -> str | None:
    """The declared version-reference path, when it lives in the repo this
    service's PRs target and the delivery mode makes it required.

    Returns None whenever the bump is not something synthesis was asked to
    include — no delivery block, a directly-reconciling mode, no declared
    path, or a reference in another repo (a PR spans one repo, so the prompt
    asks for an explanation instead of a patch in that case).
    """
    delivery = getattr(service_cfg, "delivery", None)
    if delivery is None or not delivery.is_indirect:
        return None
    ref = delivery.version_ref
    if ref is None or not ref.path or not ref.repo:
        return None
    if ref.repo != service_cfg.github_repo:
        return None
    return ref.path

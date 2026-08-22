#!/usr/bin/env python
"""Run the frozen eval corpus through the real pipeline and record a pass-rate.

This is the operator-facing entry point for the eval / ground-truth flywheel
(ROADMAP Track B). It replays every scenario in eval/scenarios/ through the
production pipeline using the real Anthropic models, scores each outcome
against the scenario's ground truth, persists one `eval_runs` row (so the
dashboard's pass-rate trend grows), and prints a summary table.

Usage:
    .venv/bin/python scripts/run_eval.py
    .venv/bin/python scripts/run_eval.py --codebase demo-app --no-persist
    .venv/bin/python scripts/run_eval.py --scenario-id oci-chart-undeliverable-patch

Every replay costs live tokens, so `--scenario-id` narrows the run while
iterating on one scenario (or on the prompt it exercises). Filtered runs are
never persisted — see `_amain`.

Deterministic/offline cousin: tests inject AsyncMock stages via eval.runner
directly; this script wires the live clients. No PR is ever opened.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow `python scripts/run_eval.py` (which puts scripts/ on sys.path, not the
# repo root) to import the `eval` and `src` packages.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.corpus import (  # noqa: E402
    UnknownScenarioError,
    corpus_version,
    load_corpus,
    select_scenarios,
)
from eval.runner import replay_scenario
from eval.scoring import aggregate, score_run
from src.agent.investigator import SYSTEM_PROMPT as INVESTIGATION_PROMPT, Investigator
from src.agent.packs import load_packs_into
from src.agent.patch_generator import SYSTEM_PROMPT as SYNTHESIS_PROMPT, PatchGenerator
from src.agent.prompts import (
    STAGE_INVESTIGATION,
    STAGE_SYNTHESIS,
    STAGE_TRIAGE,
    PromptResolver,
)
from src.agent.triage import SYSTEM_PROMPT as TRIAGE_PROMPT, Triage
from src.config import config
from src.db import create_engine, get_sessionmaker, init_schema
from src.db import repository as repo
from src.services_registry import ServiceConfig, ServiceRegistry

log = logging.getLogger("llopster.eval")


def _build_services(codebase_path: str) -> ServiceRegistry:
    """Default registry for the demo-app scenarios; point that service at the
    frozen in-repo codebase so synthesis sees the buggy source.

    Scenarios that declare their own `service:` block (those exercising an
    operator declaration such as `delivery` or `chart_lineage`) override this
    per replay — see eval/runner.replay_scenario.
    """
    return ServiceRegistry.from_mapping(
        {
            "demo-app": ServiceConfig(
                name="demo-app",
                codebase_path=codebase_path,
                github_repo="synchrony-solutions/llopster-demo",
            )
        }
    )


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    scenarios = load_corpus(args.scenarios)
    if not scenarios:
        log.error("no scenarios found — nothing to evaluate")
        return 1

    try:
        scenarios = select_scenarios(scenarios, args.scenario_id)
    except UnknownScenarioError as e:
        log.error("%s", e)
        return 3

    filtered = bool(args.scenario_id)
    if filtered:
        log.info(
            "running %d of the corpus's scenarios: %s",
            len(scenarios), ", ".join(s.id for s in scenarios),
        )
        if args.persist:
            # A filtered run is a debugging aid, not a baseline. Recording one
            # would put a row in the pass-rate trend whose numbers describe a
            # hand-picked subset — "2/2 correct" sitting next to full-corpus
            # results invites exactly the wrong conclusion.
            log.warning(
                "--scenario-id given: NOT persisting an eval_runs row "
                "(a partial run would distort the pass-rate trend). "
                "Re-run without --scenario-id to record a baseline."
            )
            args.persist = False

    if not config.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set — cannot run a live eval")
        return 2

    engine = create_engine(config.database_url)
    sessionmaker = get_sessionmaker(engine)
    await init_schema(engine)

    services = _build_services(args.codebase)

    resolver = PromptResolver(
        {
            STAGE_TRIAGE: TRIAGE_PROMPT,
            STAGE_INVESTIGATION: INVESTIGATION_PROMPT,
            STAGE_SYNTHESIS: SYNTHESIS_PROMPT,
        }
    )
    load_packs_into(resolver, config.packs_dir)

    patcher = PatchGenerator(
        api_key=config.anthropic_api_key,
        model=config.anthropic_model,
        extended_cache_ttl=config.extended_cache_ttl,
        prompt_resolver=resolver,
    )
    triage = (
        Triage(
            api_key=config.anthropic_api_key,
            model=config.anthropic_triage_model,
            prompt_resolver=resolver,
        )
        if not args.no_triage
        else None
    )
    investigator = Investigator(
        api_key=config.anthropic_api_key,
        model=config.anthropic_investigation_model,
        extended_cache_ttl=config.extended_cache_ttl,
        prompt_resolver=resolver,
    )

    scores = []
    for scenario in scenarios:
        log.info("replaying scenario %s", scenario.id)
        run = await replay_scenario(
            scenario,
            sessionmaker=sessionmaker,
            services=services,
            patcher=patcher,
            triage=triage,
            investigator=investigator,
        )
        score = score_run(scenario, run)
        scores.append(score)
        log.info("  → %s (%s)", score.label, score.reason)

    corpus = aggregate(scores)

    if args.persist:
        async with sessionmaker() as session:
            await repo.record_eval_run(
                session,
                scenario_count=corpus.scenario_count,
                correct_count=corpus.correct_count,
                partial_count=corpus.partial_count,
                wrong_count=corpus.wrong_count,
                results=[s.to_dict() for s in corpus.scores],
                corpus_version=corpus_version(scenarios),
                model=config.anthropic_model,
                trigger_source="cli",
                note=args.note,
            )

    await engine.dispose()

    _print_summary(corpus)
    return 0


def _print_summary(corpus) -> None:  # noqa: ANN001
    print("\n=== LLopster eval results ===")
    for s in corpus.scores:
        mark = {"correct": "PASS", "partial": "PART", "wrong": "FAIL"}[s.label]
        print(f"  [{mark}] {s.scenario_id:<28} {s.reason}")
    print(
        f"\n  {corpus.correct_count}/{corpus.scenario_count} correct "
        f"({corpus.partial_count} partial, {corpus.wrong_count} wrong) "
        f"— pass-rate {corpus.pass_rate:.0%}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LLopster eval corpus.")
    parser.add_argument("--scenarios", default=None, help="scenarios dir (default eval/scenarios)")
    parser.add_argument(
        "--scenario-id", action="append", default=None, metavar="ID",
        help=(
            "only replay these scenario ids (repeatable, or comma-separated). "
            "Every replay costs live tokens. Filtered runs are never persisted."
        ),
    )
    parser.add_argument("--codebase", default="demo-app", help="demo-app codebase path (default ./demo-app)")
    parser.add_argument("--note", default=None, help="optional note stored on the eval run")
    parser.add_argument("--no-triage", action="store_true", help="skip the Haiku triage stage")
    parser.add_argument(
        "--no-persist", dest="persist", action="store_false",
        help="don't write an eval_runs row (dry run)",
    )
    parser.set_defaults(persist=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()

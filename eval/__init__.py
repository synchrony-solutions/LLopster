"""Deterministic eval / ground-truth flywheel for LLopster.

This package turns the testbed bugs into a *frozen regression corpus* and
replays them through the real pipeline, scoring each outcome against a
ground-truth label. It is the consume-half of the eval flywheel (ROADMAP
Track B / BUSINESS_PLAN §3): operator labels accumulate on real runs, and
this harness produces an accumulating pass-rate trend over a stable corpus.

Layout:
  eval/scenarios/<id>/scenario.yaml  — frozen alert + recorded Loki/Prom
                                        context + ground truth (one per bug)
  eval/corpus.py                     — load + parse scenarios
  eval/runner.py                     — replay a scenario through process_alert
  eval/scoring.py                    — score a completed Run vs ground truth

The replay is deterministic and offline: recorded context is served from the
scenario file (no live Loki/Prom), no PR is ever opened, and the LLM client is
injected by the caller (real Anthropic from scripts/run_eval.py; AsyncMock in
tests — same pattern as the unit suite).
"""

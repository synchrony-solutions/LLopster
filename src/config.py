"""Centralized config loaded from environment variables."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    loki_url: str = os.getenv("LOKI_URL", "http://localhost:3100")
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    log_lookback_minutes: int = int(os.getenv("LOG_LOOKBACK_MINUTES", "30"))
    max_log_lines: int = int(os.getenv("MAX_LOG_LINES", "200"))

    # Loki stream labels the context collector probes, in priority order, to
    # scope a log query to the alerting workload. The first label present on
    # the alert wins. Deploy-time tunable (it's a property of how the
    # cluster's log collector labels streams, not a per-run setting): a stack
    # shipping logs via Grafana Alloy / Promtail with the k8s-recommended
    # labels can set e.g. "app_kubernetes_io_name,namespace,pod". Default
    # covers the common collector label schemes (raw `app`, the
    # `app.kubernetes.io/*` recommended set — dots/slashes become underscores
    # as Loki stream labels — and the bare k8s identifiers).
    log_scope_labels: tuple[str, ...] = tuple(
        label.strip()
        for label in os.getenv(
            "LOG_SCOPE_LABELS",
            "service,app,app_kubernetes_io_name,app_kubernetes_io_instance,container,pod,namespace,job",
        ).split(",")
        if label.strip()
    )

    # Per-service codebase + GitHub repo lookup. See config/services.yaml.
    services_config_path: str = os.getenv("SERVICES_CONFIG", "config/services.yaml")

    # LLM provider selector. "anthropic" (default) = the direct Anthropic
    # API keyed by ANTHROPIC_API_KEY. "bedrock" = Claude via AWS Bedrock
    # (credentials via the standard boto3 chain — IRSA / pod-identity on
    # EKS, or the static AWS_* keys below as a fallback). See
    # src/agent/llm_provider.py. Unknown values fall back to "anthropic".
    llm_provider: str = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")

    # ---- AWS Bedrock provider ------------------------------------------
    # Used only when LLM_PROVIDER=bedrock. Region is required (Bedrock is
    # regional); the static keys are an OPTIONAL fallback for clusters
    # without IRSA / pod-identity — leave them empty to use the ambient
    # boto3 credential chain (the recommended EKS path). Model IDs are the
    # Bedrock inference-profile IDs, which differ from the bare direct-API
    # names (e.g. `us.anthropic.claude-opus-4-7-v1:0`). An empty override
    # falls back to the matching direct-API model string.
    aws_region: str = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", ""))
    aws_access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_session_token: str = os.getenv("AWS_SESSION_TOKEN", "")
    bedrock_model: str = os.getenv("BEDROCK_MODEL", "us.anthropic.claude-opus-4-7-v1:0")
    bedrock_triage_model: str = os.getenv(
        "BEDROCK_TRIAGE_MODEL", "us.anthropic.claude-haiku-4-5-v1:0"
    )
    bedrock_investigation_model: str = os.getenv(
        "BEDROCK_INVESTIGATION_MODEL", "us.anthropic.claude-sonnet-4-6-v1:0"
    )

    # Haiku triage gate — cheap pre-flight call that decides whether an
    # alert is worth running the full Opus pipeline. Runtime-overridable
    # via the `triage_enabled`, `triage_model`, and `triage_min_confidence`
    # settings so operators can disable / retune without a redeploy.
    triage_enabled: bool = os.getenv("TRIAGE_ENABLED", "true").lower() in {"true", "1", "yes", "on"}
    anthropic_triage_model: str = os.getenv("ANTHROPIC_TRIAGE_MODEL", "claude-haiku-4-5")
    triage_min_confidence: int = int(os.getenv("TRIAGE_MIN_CONFIDENCE", "4"))

    # Sonnet investigation — runs after Loki/Prom collection and produces
    # a root-cause hypothesis + affected-files list. Phase B records it
    # on the Run row; Phase C will feed `affected_files` into Opus to
    # narrow its prompt. Runtime-overridable via the `investigation_*`
    # settings keys.
    investigation_enabled: bool = os.getenv("INVESTIGATION_ENABLED", "true").lower() in {"true", "1", "yes", "on"}
    anthropic_investigation_model: str = os.getenv("ANTHROPIC_INVESTIGATION_MODEL", "claude-sonnet-4-6")

    slack_webhook_url: str = os.getenv("SLACK_WEBHOOK_URL", "")

    # GitHub token is global; per-service repo comes from services.yaml.
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    patch_confidence_threshold: int = int(os.getenv("PATCH_CONFIDENCE_THRESHOLD", "4"))

    # Open PRs as drafts by default (least-privilege, propose-only posture):
    # an LLM-authored diff lands as a draft for human review rather than a
    # ready-to-merge PR. Opt out with OPEN_PRS_AS_DRAFT=false, or per-runtime
    # via the `open_prs_as_draft` setting.
    open_prs_as_draft: bool = os.getenv("OPEN_PRS_AS_DRAFT", "true").lower() in {"true", "1", "yes", "on"}

    # Use the extended (1-hour) prompt-cache TTL beta. Default on — the
    # codebase blob only changes on deploy, so 5-min TTL forces constant
    # cache rebuilds for low-traffic agents and burns cost. Opt out by
    # setting EXTENDED_CACHE_TTL=false (e.g. if your account doesn't have
    # the beta enabled).
    extended_cache_ttl: bool = os.getenv("EXTENDED_CACHE_TTL", "true").lower() in {"true", "1", "yes", "on"}

    # Automatic cost circuit breaker. When the number of runs created in the
    # trailing hour, or the estimated synthesis spend in the trailing day,
    # reaches one of these ceilings, the pipeline trips processing_mode=manual
    # (new alerts park at `queued` for operator review) and short-circuits the
    # current run before any LLM call. Runtime overridable via the
    # `max_runs_per_hour` / `max_usd_per_day` settings; 0 = that ceiling off.
    #
    # These ship NON-ZERO by default: the product's origin incident was burning
    # a month of API credits in 24h with no cap, so an out-of-the-box safety net
    # matters more than never surprising an operator. The defaults are a
    # conservative net (well above normal alert volume), NOT a tuned budget — a
    # loud startup log (`log_cost_breaker_status`) tells operators to raise them
    # to match their real volume/budget.
    max_runs_per_hour: int = int(os.getenv("MAX_RUNS_PER_HOUR", "50"))
    max_usd_per_day: float = float(os.getenv("MAX_USD_PER_DAY", "25"))

    # Post-firing backoff. A below-confidence-threshold / no-patch alert never
    # opens a PR, so the open-PR dedup never matches it and every re-firing would
    # otherwise re-run the whole Haiku→Sonnet→Opus pipeline. After a real
    # pipeline run for an alert finishes WITHOUT opening a PR, suppress further
    # firings of the same alert for this many minutes (one re-investigation per
    # window). Runtime overridable via the `patch_backoff_minutes` setting;
    # 0 = disabled.
    patch_backoff_minutes: int = int(os.getenv("PATCH_BACKOFF_MINUTES", "60"))

    # Run history retention. The pruner background task deletes runs older
    # than this many days. Set to 0 to disable pruning entirely (keep
    # forever — fine for dev, not fine for any production deployment).
    # Runtime-overridable via the `run_retention_days` setting.
    run_retention_days: int = int(os.getenv("RUN_RETENTION_DAYS", "90"))
    # How often the pruner wakes up to check for old rows. One hour is
    # frequent enough to keep the table size bounded without burning
    # write cycles.
    run_prune_interval_seconds: int = int(os.getenv("RUN_PRUNE_INTERVAL_SECONDS", "3600"))

    # Run history database. Defaults to a local SQLite file for dev;
    # production Helm chart overrides to postgresql+asyncpg://...
    database_url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/llopster.db")

    # Agent base URL — used by the dashboard to link operators to the agent's
    # /trigger page when manual triggering is needed. Set to the agent's
    # cluster-internal or external URL. Empty string hides the Trigger link.
    agent_url: str = os.getenv("AGENT_URL", "")

    # Shared secret guarding the inbound write surfaces: the AlertManager
    # /webhook, the manual-trigger routes, and the dashboard's settings/license
    # mutations. Empty = auth DISABLED (a loud startup warning is logged) so
    # local eval keeps working; set it before exposing the agent or dashboard
    # on an untrusted network. A runtime override in the Setting table (key
    # ``api_auth_token``) takes precedence over this env var, mirroring the
    # license-key pattern. Accepted by clients as `Authorization: Bearer
    # <token>` or HTTP Basic (the password component) — so AlertManager,
    # curl, the dashboard→agent proxy, and a browser all work.
    api_auth_token: str = os.getenv("LLOPSTER_API_TOKEN", "")

    # License key (signed JWT). Source of truth for paid-feature gating —
    # see src/agent/license.py. Mounted from a Secret in the chart (the same
    # pattern as ANTHROPIC_API_KEY / GITHUB_TOKEN). Absent = Community tier
    # (the default for every Community deployment; gates nothing). A runtime
    # override in the Setting table (key ``license_key``) takes precedence
    # over this env var, mirroring the rest of the settings-override knobs.
    license_key: str = os.getenv("LLOPSTER_LICENSE_KEY", "")

    # Premium-pack mount. At startup the agent scans this dir for closed
    # content packs (tuned prompts) and overlays them on the baked-in
    # Community prompts when entitled — see src/agent/packs.py. Absent dir =
    # Community prompts only (the default for every Community deployment).
    packs_dir: str = os.getenv("LLOPSTER_PACKS_DIR", "/packs")

    @property
    def llm_configured(self) -> bool:
        """Whether the active LLM provider has enough config to build a
        client. For the direct Anthropic API that means an API key is set;
        for Bedrock we always return True and let credentials resolve
        (IRSA / static keys) at call time — a missing region only logs a
        warning. Drives the patcher/triage/investigator wiring in main.py
        and the dashboard's connection-status surfaces."""
        if self.llm_provider == "bedrock":
            return True
        return bool(self.anthropic_api_key)


config = Config()

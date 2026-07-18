"""Premium-pack discovery + loading.

A *pack* is a versioned, signed bundle of premium content that ships OUTSIDE
this source-available repo (in a separate private repo / registry) and is
mounted into the agent pod at runtime. The engine in this repo ships only the
loader, the schema, and an empty example pack for tests — never any premium
content itself.

Pack layout (on disk, under ``LLOPSTER_PACKS_DIR``, default ``/packs``)
-----------------------------------------------------------------------
::

    <packs-dir>/
      mypack/
        manifest.yaml        # required — see schema below
        prompts/             # optional — one file per stage it overlays
          synthesis.md       # overlay for the patch_generator system prompt
          investigation.md   # overlay for the investigator system prompt
          triage.md          # overlay for the triage system prompt
        alerts/              # optional — curated PrometheusRule YAML, applied
          jvm.yaml           #   by the OPERATOR (kubectl/helm), NOT this engine

``manifest.yaml`` schema
------------------------
::

    id: jvm-pack             # required, unique; the entitlement key
    version: 1.2.0           # required
    stack: jvm               # optional; the ServiceConfig.pack value that
                             #   selects this pack. Omit/null = applies to
                             #   services with no `pack` field set.
    signature: "sha256:..."  # required; integrity stub (see below)
    prompts:                 # optional; stage -> filename under prompts/
      synthesis: synthesis.md
      investigation: investigation.md
      triage: triage.md

Only the ``prompts`` mapping is consumed by the engine. ``alerts/`` is a
content-distribution convention applied by the operator; this loader ignores
it (no engine code is needed to ship alert rules).

Signature stub
--------------
This is NOT real crypto yet. The interim signature is a deterministic digest
over ``id`` + ``version`` (see ``compute_stub_signature``). A pack whose
``signature`` doesn't match is skipped (fail-open to Community prompts). This
exists to exercise the verify path and give pack-build tooling a hook; it will
be replaced by real artifact signing when the packs CI / private repo lands.
Entitlement (who is *allowed* to apply a pack) is a separate concern handled by
``entitlements.is_pack_enabled`` at resolve time — a pack can be present and
signature-valid yet still fall back to Community if the deployment isn't
entitled.

Failure handling
----------------
Every failure mode — missing dir, unreadable manifest, missing required field,
bad signature, missing prompt file, unknown stage — is logged at WARNING and
the offending pack is skipped. Loading never raises; a broken pack can never
take down startup or the pipeline.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import yaml

from src.agent.prompts import ALL_STAGES, PackPrompt, PromptResolver

log = logging.getLogger("llopster.packs")

DEFAULT_PACKS_DIR = "/packs"


class PackError(Exception):
    """A pack is malformed / unverifiable. Caught per-pack; never propagates
    out of ``load_packs_into``."""


def compute_stub_signature(pack_id: str, version: str) -> str:
    """Deterministic integrity-stub signature over a pack's id+version.

    Interim only — real artifact signing replaces this when the packs CI
    lands. Kept here (rather than hidden) so pack-build tooling and tests can
    generate a valid signature.
    """
    digest = hashlib.sha256(f"{pack_id}:{version}".encode()).hexdigest()
    return f"sha256:{digest}"


def _verify_signature(pack_id: str, version: str, signature: str) -> bool:
    return bool(signature) and signature == compute_stub_signature(pack_id, version)


def load_packs_into(resolver: PromptResolver, packs_dir: str | None) -> int:
    """Scan ``packs_dir`` and register each valid pack's prompt overlays on
    ``resolver``. Returns the number of packs successfully registered.

    Fail-open throughout: an absent dir, or any individual broken pack, is
    logged and skipped — never raised.
    """
    if not packs_dir:
        return 0
    base = Path(packs_dir)
    if not base.exists() or not base.is_dir():
        log.info(
            "packs: dir %s not present — Community prompts only (no packs loaded)",
            base,
        )
        return 0

    loaded = 0
    for pack_dir in sorted(base.iterdir()):
        if not pack_dir.is_dir():
            continue
        try:
            if _load_one_pack(resolver, pack_dir):
                loaded += 1
        except PackError as e:
            log.warning("packs: skipping %s — %s", pack_dir.name, e)
        except Exception as e:  # never let a surprise crash startup
            log.warning(
                "packs: skipping %s — unexpected error: %s", pack_dir.name, e,
            )
    log.info("packs: loaded %d pack(s) from %s", loaded, base)
    return loaded


def _load_one_pack(resolver: PromptResolver, pack_dir: Path) -> bool:
    """Parse + verify a single pack dir and register its overlays.

    Returns True if at least one overlay was registered. Raises PackError on
    any validation failure (caught by the caller).
    """
    manifest_path = pack_dir / "manifest.yaml"
    if not manifest_path.is_file():
        raise PackError("no manifest.yaml")

    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise PackError(f"manifest is not valid YAML: {e}") from e
    if not isinstance(manifest, dict):
        raise PackError("manifest is not a mapping")

    pack_id = manifest.get("id")
    version = manifest.get("version")
    signature = manifest.get("signature", "")
    stack = manifest.get("stack")  # optional; None applies to no-pack services
    if not pack_id or not version:
        raise PackError("manifest missing required 'id' or 'version'")
    pack_id = str(pack_id)
    version = str(version)
    stack = str(stack) if stack is not None else None

    if not _verify_signature(pack_id, version, str(signature)):
        raise PackError(f"signature verification failed for pack {pack_id!r}")

    prompts_map = manifest.get("prompts") or {}
    if not isinstance(prompts_map, dict):
        raise PackError("manifest 'prompts' is not a mapping")

    registered = 0
    for stage, filename in prompts_map.items():
        if stage not in ALL_STAGES:
            log.warning(
                "packs: pack %r declares unknown stage %r — skipping that "
                "overlay", pack_id, stage,
            )
            continue
        prompt_path = pack_dir / "prompts" / str(filename)
        try:
            resolved = prompt_path.resolve()
            resolved.relative_to((pack_dir / "prompts").resolve())
        except (ValueError, OSError):
            log.warning(
                "packs: pack %r prompt path %r escapes prompts/ — skipping",
                pack_id, filename,
            )
            continue
        if not prompt_path.is_file():
            log.warning(
                "packs: pack %r references missing prompt file %s — skipping",
                pack_id, prompt_path,
            )
            continue
        text = prompt_path.read_text().strip()
        if not text:
            log.warning(
                "packs: pack %r prompt %s is empty — skipping", pack_id, filename,
            )
            continue
        resolver.register_overlay(
            PackPrompt(
                pack_id=pack_id,
                version=version,
                stage=stage,
                stack=stack,
                text=text,
            )
        )
        registered += 1

    if registered == 0:
        raise PackError(f"pack {pack_id!r} registered no usable prompt overlays")
    return True

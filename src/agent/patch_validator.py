"""Independent post-patch validation gate.

This is the only gate between an LLM-authored patch and a PR that does **not**
rely on the model's own self-scored confidence. A diff can apply cleanly (every
context line matches) and still leave a file that no longer parses — the model
misplaced a bracket, broke YAML indentation, or dropped a quote. Self-reported
confidence is highest exactly when the model has misdiagnosed with conviction,
so we run a cheap, model-independent sanity check on the *patched content* of
every touched file and fail the run closed if any check fails.

The checks are in-process and always available (no external binaries, no chart
context needed):

  * ``.py``            → ``compile()`` syntax check (raises SyntaxError)
  * ``.yaml`` / ``.yml`` → ``yaml.safe_load_all`` parse check
  * ``.json``          → ``json.loads`` parse check

Files with an extension we can't check pass through — the gate only *fails* on a
check it can actually run and that actually failed (never a false block). This
is deliberately narrower than a ``helm lint`` / ``kubeconform`` schema
validation would be: those need the binaries in-image and full
chart context, and the patch path-gate already blocks ``helm-chart/`` templates
from being patched at all — so a per-file parse check is the right-fit gate for
the app source and config the agent actually touches. Binary-backed schema
validators can be layered in here later behind ``shutil.which`` without changing
the call site.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("llopster.patch_validator")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)  # "path: message" per failure


def _check_python(path: str, content: str) -> str | None:
    try:
        compile(content, path, "exec")
    except SyntaxError as e:
        return f"Python syntax error: {e.msg} (line {e.lineno})"
    return None


def _check_yaml(path: str, content: str) -> str | None:
    try:
        # safe_load_all handles multi-document files (`---` separators).
        for _ in yaml.safe_load_all(content):
            pass
    except yaml.YAMLError as e:
        return f"invalid YAML: {e}"
    return None


def _check_json(path: str, content: str) -> str | None:
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        return f"invalid JSON: {e}"
    return None


# Extension → validator. Lowercased extension including the leading dot.
_VALIDATORS = {
    ".py": _check_python,
    ".yaml": _check_yaml,
    ".yml": _check_yaml,
    ".json": _check_json,
}


def _extension(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def validate_patched_file(path: str, content: str) -> str | None:
    """Return an error string if the patched content is invalid, else None.

    Unknown extensions return None (not validated — never a false failure)."""
    validator = _VALIDATORS.get(_extension(path))
    if validator is None:
        return None
    return validator(path, content)


def validate_patched_files(files: list[tuple[str, str]]) -> ValidationResult:
    """Validate every (path, patched_content) pair. Fail closed on any error."""
    errors: list[str] = []
    for path, content in files:
        err = validate_patched_file(path, content)
        if err is not None:
            errors.append(f"{path}: {err}")
    return ValidationResult(ok=not errors, errors=errors)

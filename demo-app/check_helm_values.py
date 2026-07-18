"""Validates Kubernetes-style memory units in the packaged helm-values.yaml.

Runs on a loop; each validation failure pushes a log line to Loki and
increments a Prometheus counter that an alert rule watches.
"""

import os
import re
import time

import yaml
from prometheus_client import Counter

VALUES_PATH = os.getenv("VALUES_PATH", "/app/helm-values.yaml")
RETRY_SECONDS = int(os.getenv("RETRY_SECONDS", "10"))

config_errors = Counter(
    "demo_app_config_errors_total",
    "Total number of helm-values parse failures",
)

# K8s memory units: Ki, Mi, Gi, Ti, Pi, Ei (binary) or K, M, G, T, P, E (decimal),
# or a bare integer (bytes). Anything else is invalid.
_VALID_MEMORY = re.compile(r"^\d+(Ki|Mi|Gi|Ti|Pi|Ei|K|M|G|T|P|E)?$")


class ConfigError(ValueError):
    pass


def validate_memory(value: str) -> None:
    if not _VALID_MEMORY.match(value):
        raise ConfigError(
            f"invalid memory value {value!r}: must be a number optionally followed by "
            "Ki/Mi/Gi/Ti/Pi/Ei or K/M/G/T/P/E"
        )


def check_once() -> None:
    with open(VALUES_PATH) as f:
        values = yaml.safe_load(f)
    mem = values["resources"]["limits"]["memory"]
    validate_memory(mem)


def run(push_log) -> None:
    while True:
        try:
            check_once()
            print("[info] helm_values: config valid", flush=True)
        except Exception as e:
            config_errors.inc()
            line = f"ERROR validating helm values from {VALUES_PATH}: {e}"
            print(line, flush=True)
            push_log(line, level="error", component="helm_values")
        time.sleep(RETRY_SECONDS)

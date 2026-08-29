"""Startup gating for read-only cluster access.

The point of these: `clusterContext.enabled=false` is the shipping default and
must mean *no client is constructed at all* — not a client that happens never
to be called.
"""

from dataclasses import replace

import pytest

from src.api import main as main_module
from src.config import config as real_config


@pytest.fixture
def cfg(monkeypatch):
    """Swap the module-level config the builder reads."""

    def apply(**overrides):
        patched = replace(real_config, **overrides)
        monkeypatch.setattr(main_module, "config", patched)
        return patched

    return apply


@pytest.fixture(autouse=True)
def in_a_pod(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")


def test_disabled_builds_no_client_and_no_collector(cfg):
    cfg(cluster_context_enabled=False)
    http, collector = main_module._build_cluster_collector()
    assert http is None
    assert collector is None


def test_enabled_outside_a_pod_degrades_instead_of_failing(cfg, monkeypatch, caplog):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    cfg(cluster_context_enabled=True, cluster_context_namespaces=("prod",))
    with caplog.at_level("WARNING"):
        http, collector = main_module._build_cluster_collector()
    assert (http, collector) == (None, None)
    assert "not running in a pod" in caplog.text


def test_enabled_builds_a_collector_scoped_to_the_configured_namespaces(cfg):
    cfg(
        cluster_context_enabled=True,
        cluster_context_namespaces=("prod", "payments"),
        cluster_context_all_namespaces=False,
        cluster_context_max_events=7,
        cluster_context_include_pod_spec=False,
        cluster_context_max_pod_spec_bytes=1234,
    )
    http, collector = main_module._build_cluster_collector()
    try:
        assert collector is not None
        assert collector.namespaces == ("prod", "payments")
        assert collector.all_namespaces is False
        assert collector.max_events == 7
        assert collector.include_pod_spec is False
        assert collector.max_pod_spec_bytes == 1234
        # Cluster-scoped reads (nodes) require a ClusterRoleBinding, which the
        # chart only creates for allNamespaces.
        assert collector.cluster_scoped_reads is False
        assert collector.client.base_url == "https://10.96.0.1:443"
    finally:
        if http is not None:
            import anyio

            anyio.run(http.aclose)


def test_enabled_with_no_permitted_namespaces_warns_loudly(cfg, caplog):
    cfg(
        cluster_context_enabled=True,
        cluster_context_namespaces=(),
        cluster_context_all_namespaces=False,
    )
    with caplog.at_level("WARNING"):
        http, collector = main_module._build_cluster_collector()
    try:
        assert collector is not None  # built, but it can read nothing
        assert "no namespaces are permitted" in caplog.text
    finally:
        if http is not None:
            import anyio

            anyio.run(http.aclose)

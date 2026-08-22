"""Tests for the service registry."""

import textwrap

import pytest

from src.services_registry import ServiceRegistry


def test_loads_valid_services(tmp_path):
    cfg = tmp_path / "services.yaml"
    cfg.write_text(textwrap.dedent("""
        demo-app:
          codebase_path: ./demo-app
          github_repo: org/demo-app
        payments:
          codebase_path: /repos/payments
          github_repo: org/payments
    """))
    reg = ServiceRegistry(str(cfg))
    assert len(reg) == 2
    demo = reg.get("demo-app")
    assert demo is not None
    assert demo.codebase_path == "./demo-app"
    assert demo.github_repo == "org/demo-app"
    assert reg.get("payments").codebase_path == "/repos/payments"


def test_returns_none_for_unknown_service(tmp_path):
    cfg = tmp_path / "services.yaml"
    cfg.write_text("demo-app:\n  codebase_path: ./d\n  github_repo: org/d\n")
    reg = ServiceRegistry(str(cfg))
    assert reg.get("never-heard-of-it") is None


def test_skips_entries_missing_required_fields(tmp_path):
    cfg = tmp_path / "services.yaml"
    cfg.write_text(textwrap.dedent("""
        good:
          codebase_path: ./g
          github_repo: org/g
        bad-no-repo:
          codebase_path: ./b
        bad-no-path:
          github_repo: org/b
    """))
    reg = ServiceRegistry(str(cfg))
    assert reg.get("good") is not None
    assert reg.get("bad-no-repo") is None
    assert reg.get("bad-no-path") is None
    assert len(reg) == 1


def test_missing_file_yields_empty_registry(tmp_path):
    reg = ServiceRegistry(str(tmp_path / "does-not-exist.yaml"))
    assert len(reg) == 0
    assert reg.get("anything") is None


def test_malformed_yaml_yields_empty_registry(tmp_path):
    cfg = tmp_path / "services.yaml"
    cfg.write_text("this is: : not valid: yaml::")
    reg = ServiceRegistry(str(cfg))
    assert len(reg) == 0


def test_pack_field_is_optional_and_backward_compatible(tmp_path):
    cfg = tmp_path / "services.yaml"
    cfg.write_text(textwrap.dedent("""
        with-pack:
          codebase_path: ./w
          github_repo: org/w
          pack: jvm
        without-pack:
          codebase_path: ./o
          github_repo: org/o
    """))
    reg = ServiceRegistry(str(cfg))
    assert reg.get("with-pack").pack == "jvm"
    # Absent field defaults to None (Community prompts) — old configs unchanged.
    assert reg.get("without-pack").pack is None


def test_names_returns_sorted_list(tmp_path):
    cfg = tmp_path / "services.yaml"
    cfg.write_text(textwrap.dedent("""
        zeta:
          codebase_path: ./z
          github_repo: org/z
        alpha:
          codebase_path: ./a
          github_repo: org/a
    """))
    reg = ServiceRegistry(str(cfg))
    assert reg.names() == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# Optional `delivery` (#24 part B) and `chart_lineage` (#25 option C) blocks.
# Both are advisory: absent or malformed, the service still loads and behaves
# exactly as it did before these fields existed.
# ---------------------------------------------------------------------------

def _write(tmp_path, body: str) -> ServiceRegistry:
    cfg = tmp_path / "services.yaml"
    cfg.write_text(body)
    return ServiceRegistry(str(cfg))


FLAT_ENTRY = """\
demo-app:
  codebase_path: /codebases/demo-app
  github_repo: acme/demo-app
"""


def test_flat_entry_unchanged_by_the_new_fields(tmp_path):
    """Regression guard: the pre-existing schema keeps loading identically."""
    svc = _write(tmp_path, FLAT_ENTRY).get("demo-app")
    assert svc.codebase_path == "/codebases/demo-app"
    assert svc.github_repo == "acme/demo-app"
    assert svc.delivery is None
    assert svc.chart_lineage == ()


def test_delivery_block_parsed_with_version_ref(tmp_path):
    svc = _write(tmp_path, FLAT_ENTRY + """\
  delivery:
    mode: oci-chart
    version_ref:
      repo: acme/platform-deployment
      path: clusters/prod/values-overrides.yaml
      key: airflow.image.tag
""").get("demo-app")
    assert svc.delivery.mode == "oci-chart"
    assert svc.delivery.is_indirect is True
    assert svc.delivery.version_ref.repo == "acme/platform-deployment"
    assert svc.delivery.version_ref.key == "airflow.image.tag"


def test_git_manifest_mode_is_not_indirect(tmp_path):
    svc = _write(tmp_path, FLAT_ENTRY + """\
  delivery:
    mode: git-manifest
""").get("demo-app")
    assert svc.delivery.mode == "git-manifest"
    assert svc.delivery.is_indirect is False


def test_indirect_mode_without_version_ref_still_loads(tmp_path):
    """Knowing the mode is most of the value — the ref is a bonus."""
    svc = _write(tmp_path, FLAT_ENTRY + """\
  delivery:
    mode: image-build
""").get("demo-app")
    assert svc.delivery.is_indirect is True
    assert svc.delivery.version_ref is None


@pytest.mark.parametrize(
    "block",
    [
        "  delivery:\n    mode: oci_chart\n",      # underscore typo
        "  delivery:\n    mode: ''\n",             # empty
        "  delivery:\n    mode: nonsense\n",
        "  delivery: not-a-mapping\n",
        "  delivery:\n    version_ref:\n      key: a.b\n",   # no mode
    ],
)
def test_malformed_delivery_drops_the_block_but_keeps_the_service(tmp_path, block):
    """An optional-field typo must not pull a service out of monitoring."""
    svc = _write(tmp_path, FLAT_ENTRY + block).get("demo-app")
    assert svc is not None
    assert svc.github_repo == "acme/demo-app"
    assert svc.delivery is None


def test_chart_lineage_parsed_in_order_with_visibility(tmp_path):
    svc = _write(tmp_path, FLAT_ENTRY + """\
  chart_lineage:
    - {name: resrv, version: 2.5.0, repo: acme/platform-charts, visible: false}
    - {name: airflow-tool, version: 2.1.0, repo: acme/platform-charts, visible: true}
    - {name: airflow, version: 1.19.0, repo: apache/airflow}
""").get("demo-app")
    assert [layer.name for layer in svc.chart_lineage] == [
        "resrv", "airflow-tool", "airflow",
    ]
    assert [layer.visible for layer in svc.chart_lineage] == [False, True, False]
    # YAML renders `2.5.0` as a string but `1.19` would be a float — everything
    # scalar is stringified so the prompt never shows `2.5` for `2.50`.
    assert svc.chart_lineage[0].version == "2.5.0"
    assert svc.chart_lineage[2].repo == "apache/airflow"


def test_chart_lineage_skips_bad_entries_but_keeps_good_ones(tmp_path):
    svc = _write(tmp_path, FLAT_ENTRY + """\
  chart_lineage:
    - {name: good-one, version: 1.0.0}
    - "just a string"
    - {version: 2.0.0}
""").get("demo-app")
    assert [layer.name for layer in svc.chart_lineage] == ["good-one"]


def test_chart_lineage_not_a_list_is_ignored(tmp_path):
    svc = _write(tmp_path, FLAT_ENTRY + "  chart_lineage: nope\n").get("demo-app")
    assert svc is not None
    assert svc.chart_lineage == ()


def test_numeric_version_is_stringified(tmp_path):
    svc = _write(tmp_path, FLAT_ENTRY + """\
  chart_lineage:
    - {name: tool, version: 2.5}
""").get("demo-app")
    assert svc.chart_lineage[0].version == "2.5"

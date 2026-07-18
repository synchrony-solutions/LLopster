"""Tests for the service registry."""

import textwrap

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

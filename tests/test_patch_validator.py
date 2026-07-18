"""Tests for the independent post-patch validation gate."""

from src.agent.patch_validator import (
    validate_patched_file,
    validate_patched_files,
)


def test_valid_python_passes():
    assert validate_patched_file("src/foo.py", "x = 1\ndef f():\n    return x\n") is None


def test_broken_python_fails():
    err = validate_patched_file("src/foo.py", "def f(:\n    return 1\n")
    assert err is not None
    assert "syntax" in err.lower()


def test_valid_yaml_passes():
    assert validate_patched_file("config.yaml", "a: 1\nb:\n  - x\n  - y\n") is None


def test_valid_multi_doc_yaml_passes():
    assert validate_patched_file("k8s.yaml", "a: 1\n---\nb: 2\n") is None


def test_broken_yaml_fails():
    # A mapping value that opens a flow sequence and never closes it.
    err = validate_patched_file("config.yaml", "a: [1, 2\nb: 3\n")
    assert err is not None
    assert "yaml" in err.lower()


def test_valid_json_passes():
    assert validate_patched_file("data.json", '{"a": 1, "b": [2, 3]}') is None


def test_broken_json_fails():
    err = validate_patched_file("data.json", '{"a": 1,}')
    assert err is not None
    assert "json" in err.lower()


def test_unknown_extension_is_not_validated():
    # No validator for .md — never a false failure, even on nonsense content.
    assert validate_patched_file("README.md", "```\nnot code\n") is None


def test_extensionless_file_is_not_validated():
    assert validate_patched_file("Makefile", "all:\n\tgibberish") is None


def test_validate_files_aggregates_errors():
    result = validate_patched_files([
        ("ok.py", "x = 1\n"),
        ("bad.py", "def (:\n"),
        ("bad.json", "{"),
    ])
    assert result.ok is False
    assert len(result.errors) == 2
    assert any("bad.py" in e for e in result.errors)
    assert any("bad.json" in e for e in result.errors)


def test_validate_files_all_valid():
    result = validate_patched_files([("a.py", "x = 1\n"), ("b.yaml", "k: v\n")])
    assert result.ok is True
    assert result.errors == []

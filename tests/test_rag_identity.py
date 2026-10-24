"""Provenance hashes isolate selected deployed source and exact JSON requests."""

import pytest
from korsik.rag_identity import (
    _function_source_fingerprint,
    _generation_runtime_source_fingerprint,
    generation_payload_fingerprint,
    pipeline_identities,
)


def test_function_identity_excludes_unrelated_metadata_but_pins_selected_code(tmp_path):
    source = tmp_path / "retrieval.py"
    source.write_text("def chunks(text):\n    return text.split()\n")
    original = _function_source_fingerprint(source, ("chunks",), "v1")
    source.write_text(
        source.read_text() + '\nMETADATA = "new"\ndef logging():\n    return 42\n'
    )
    assert _function_source_fingerprint(source, ("chunks",), "v1") == original
    source.write_text(source.read_text().replace("text.split()", 'text.split(".")'))
    assert _function_source_fingerprint(source, ("chunks",), "v1") != original
    assert _function_source_fingerprint(source, ("chunks",), "v2") != original
    with pytest.raises(RuntimeError, match="SOURCE_MISSING"):
        _function_source_fingerprint(source, ("missing",), "v1")


def test_generation_payload_hash_canonical_json_and_changed_evidence():
    first = {"question": "Отпуск?", "evidence": [{"id": "a", "text": "14 дней"}]}
    reordered = {"evidence": [{"text": "14 дней", "id": "a"}], "question": "Отпуск?"}
    assert generation_payload_fingerprint(first) == generation_payload_fingerprint(
        reordered
    )
    reordered["evidence"][0]["text"] = "28 дней"
    assert generation_payload_fingerprint(first) != generation_payload_fingerprint(
        reordered
    )
    with pytest.raises(ValueError):
        generation_payload_fingerprint({"temperature": float("nan")})


def test_actual_deployed_identities_are_immutable_to_callers():
    actual = pipeline_identities()
    for name in (
        "chunker_fingerprint",
        "retrieval_policy_fingerprint",
        "generation_runtime_source_fingerprint",
        "retrieval_module_source_fingerprint",
    ):
        assert len(actual[name]) == 64
        int(actual[name], 16)
    assert actual["chunker_fingerprint"] != actual["retrieval_policy_fingerprint"]
    actual["chunker_version"] = "forged"
    assert pipeline_identities()["chunker_version"] != "forged"


def test_runtime_prompt_source_bytes_are_pinned(tmp_path):
    runtime = tmp_path / "model-server-app.py"
    runtime.write_text('SYSTEM_PROMPT = "only supplied evidence"\n')
    original = _generation_runtime_source_fingerprint(runtime)
    runtime.write_text('SYSTEM_PROMPT = "changed actual runtime prompt"\n')
    assert _generation_runtime_source_fingerprint(runtime) != original


def test_module_constant_change_invalidates_deployment_identity(tmp_path, monkeypatch):
    from pathlib import Path
    from korsik import rag_identity as identity

    actual = Path(identity.__file__).with_name("retrieval.py")
    copied = tmp_path / "retrieval.py"
    copied.write_bytes(actual.read_bytes())
    monkeypatch.setattr(identity, "__file__", str(tmp_path / "rag_identity.py"))
    monkeypatch.setattr(
        identity, "_generation_runtime_source_fingerprint", lambda: "0" * 64
    )
    identity._deployed_identities.cache_clear()
    try:
        first = identity.pipeline_identities()
        copied.write_text(
            copied.read_text() + '\nNEW_POLICY_VOCABULARY = {"mandatory": "must"}\n'
        )
        identity._deployed_identities.cache_clear()
        second = identity.pipeline_identities()
        assert first["chunker_fingerprint"] == second["chunker_fingerprint"]
        assert (
            first["retrieval_policy_fingerprint"]
            == second["retrieval_policy_fingerprint"]
        )
        assert (
            first["retrieval_module_source_fingerprint"]
            != second["retrieval_module_source_fingerprint"]
        )
        assert first != second
    finally:
        identity._deployed_identities.cache_clear()

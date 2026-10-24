"""Deterministic code identities, not a claim of answer faithfulness or quality."""

import ast
from functools import lru_cache
import hashlib
import json
from pathlib import Path

CHUNKER_VERSION = "korsik-physical-parent-chunker-v1"
RETRIEVAL_POLICY_VERSION = "korsik-hybrid-retrieval-guards-v1"
GENERATION_REQUEST_SCHEMA_VERSION = "korsik-local-generation-request-v1"

CHUNKER_FUNCTIONS = ("make_chunks", "_local_chunks")
RETRIEVAL_FUNCTIONS = (
    "search",
    "bm25",
    "contextual_candidate",
    "heading_topic_signal",
    "literal_normative_parent",
    "source_completeness_guard",
    "enumeration_parent",
    "answer_question",
    "validate_generated",
    "safe_history",
    "lexical_terms",
    "query_terms_with_equivalents",
    "substantive_evidence",
    "reviewed_figure_text",
    "rerank_passages",
    "critical_normative_text",
    "topic_alignment",
)


def _canonical_json(value):
    # Reject non-JSON values/NaN instead of hashing an ambiguous string fallback.
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def generation_payload_fingerprint(payload):
    """Hash exactly the JSON request object, independent of dictionary key order."""
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _function_source_fingerprint(path, function_names, version):
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: ast.get_source_segment(source, node)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = set(function_names) - functions.keys()
    if missing:
        raise RuntimeError("RAG_IDENTITY_SOURCE_MISSING: " + ",".join(sorted(missing)))
    # Names and segment boundaries are part of the input; unrelated functions,
    # imports and metadata-only edits elsewhere in the file are excluded.
    material = {
        "version": version,
        "functions": [
            {"name": name, "source": functions[name]} for name in function_names
        ],
    }
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def _generation_runtime_source_fingerprint(path=None):
    if path is None:
        deployed = Path(__file__).parent.parent / "runtime/model-server-app.py"
        path = (
            deployed
            if deployed.exists()
            else Path(__file__).resolve().parents[2] / "services/model_server/app.py"
        )
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def _deployed_identities():
    source = Path(__file__).with_name("retrieval.py")
    return {
        "chunker_version": CHUNKER_VERSION,
        "chunker_fingerprint": _function_source_fingerprint(
            source, CHUNKER_FUNCTIONS, CHUNKER_VERSION
        ),
        "retrieval_policy_version": RETRIEVAL_POLICY_VERSION,
        "retrieval_policy_fingerprint": _function_source_fingerprint(
            source, RETRIEVAL_FUNCTIONS, RETRIEVAL_POLICY_VERSION
        ),
        # This conservative deployment gate also pins module-level vocabulary,
        # constants and newly called helpers; the narrower AST IDs stay useful
        # for diagnosing which chunking/policy implementation produced data.
        "retrieval_module_source_fingerprint": hashlib.sha256(
            source.read_bytes()
        ).hexdigest(),
        "generation_request_schema_version": GENERATION_REQUEST_SCHEMA_VERSION,
        "generation_runtime_source_fingerprint": _generation_runtime_source_fingerprint(),
    }


def pipeline_identities():
    """Return identities from this deployment's selected source functions."""
    return dict(_deployed_identities())

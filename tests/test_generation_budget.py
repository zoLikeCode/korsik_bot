"""Budget/error provenance through the real handler; tiny model double, no weights."""

import hashlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import HTTPException
import pytest


@pytest.fixture
def runtime(monkeypatch):
    source = Path(__file__).parents[1] / "services/model_server/app.py"
    spec = importlib.util.spec_from_file_location("budget_test_model_runtime", source)
    app = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, app)
    spec.loader.exec_module(app)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: clock.now))

    class Tensor:
        shape = (1, 10)

        def to(self, device):
            return self

    class Inputs(dict):
        input_ids = Tensor()

    class Output:
        shape = (1, 14)

        def __init__(self, eos):
            self.tokens = [0] * 13 + [2 if eos else 9]

        def __getitem__(self, index):
            return self.tokens

    class Tokenizer:
        eos_token_id, pad_token_id = 2, 0

        def encode(self, text, **kw):
            return [1]

        def apply_chat_template(self, messages, **kw):
            assert kw["enable_thinking"] is False
            self.rendered = repr(messages)
            return self.rendered

        def __call__(self, prompt, **kw):
            clock.now += model.prepare_seconds
            return Inputs(input_ids=Tensor())

        def decode(self, tokens, **kw):
            return "Ответ по источнику [1]."

    class Model:
        device = "cpu"
        generation_config = SimpleNamespace(eos_token_id=2)
        prepare_seconds, decode_seconds, eos = 0.0, 0.1, True
        calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            self.last_budget = kwargs["max_time"]
            clock.now += self.decode_seconds
            return Output(self.eos)

    model, tok = Model(), Tokenizer()
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            manual_seed=lambda value: None,
            cuda=SimpleNamespace(is_available=lambda: False),
            inference_mode=__import__("contextlib").nullcontext,
        ),
    )
    app.ROLE = "generator"
    app.STATE.update(fingerprint="artifact", tokenizer=tok, model=model)
    return app, model, tok, clock


def request(app, **kwargs):
    return app.GenerationRequest(
        question="Когда обед?",
        evidence=[{"text": "Обед 12:00–13:00.", "title": "Правило"}],
        **kwargs
    )


def test_prepare_time_reduces_decode_budget_and_records_exact_prompt(runtime):
    app, model, tok, clock = runtime
    model.prepare_seconds, model.decode_seconds = 3, 1
    result = app.generate(request(app, request_budget_seconds=8.0))
    assert model.last_budget == 5
    assert result["request_seconds"] == 4
    assert (
        result["rendered_prompt_fingerprint"]
        == hashlib.sha256(tok.rendered.encode()).hexdigest()
    )
    assert (
        len(result["renderer_fingerprint"])
        == len(result["request_payload_fingerprint"])
        == 64
    )
    assert "12:00–13:00." in tok.rendered
    assert not app.LOCK.locked()


def test_expired_prepare_and_incomplete_decode_release_admission(runtime):
    app, model, tok, clock = runtime
    model.prepare_seconds = 3
    with pytest.raises(HTTPException) as prepared:
        app.generate(request(app, request_budget_seconds=2))
    assert prepared.value.detail["code"] == "GENERATION_BUDGET_EXCEEDED"
    assert prepared.value.detail["phase"] == "PREPARE"
    assert model.calls == 0
    model.prepare_seconds, model.decode_seconds, model.eos = 0, 6, False
    with pytest.raises(HTTPException) as decoded:
        app.generate(request(app, request_budget_seconds=5))
    assert decoded.value.detail["code"] == "GENERATION_BUDGET_EXCEEDED"
    assert decoded.value.detail["inference_seconds"] == 6
    assert decoded.value.detail["output_tokens"] == 4
    assert len(decoded.value.detail["rendered_prompt_fingerprint"]) == 64
    model.decode_seconds, model.eos = 0.1, True
    assert app.generate(request(app))["text"]
    held = [app.GENERATION_ADMISSION.acquire(blocking=False) for _ in range(9)]
    assert all(held)
    for _ in held:
        app.GENERATION_ADMISSION.release()
    assert not app.LOCK.locked()


def test_incomplete_token_limit_is_distinct_from_time_budget(runtime):
    app, model, tok, clock = runtime
    model.eos = False
    with pytest.raises(HTTPException) as failed:
        app.generate(request(app))
    assert failed.value.detail["code"] == "INCOMPLETE_GENERATION"
    with pytest.raises(ValueError):
        request(app, max_time_seconds=18)
    with pytest.raises(ValueError):
        request(app, request_budget_seconds=float("nan"))

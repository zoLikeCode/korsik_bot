"""Opt-in ISOLATED GPU0 CUDA-allocator fault wrapper; NOT a production module.

No fake exception is raised. A real CUDA allocation in the unchanged model
handler must fail under a post-load allocator cap before OOM can be recorded.
This is controlled allocator exhaustion, NOT physical VRAM exhaustion proof.
Do not import/run until root explicitly releases the GPU0 test window.
"""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import threading
import time

# The mounted operator module is outside /srv, where the unchanged image keeps
# its real app.py. Do not let Uvicorn's --app-dir select another model module.
sys.path.insert(0, "/srv")
import app as runtime
from fastapi import HTTPException
import torch


if os.environ.get("KORSIK_ALLOW_ISOLATED_ALLOCATOR_OOM") != "YES_GPU0_NO_MAX":
    raise RuntimeError("EXPLICIT_ISOLATED_GPU0_FAULT_OPT_IN_REQUIRED")
EXPECTED_UUID = os.environ.get("KORSIK_OOM_EXPECTED_GPU_UUID", "")
if not EXPECTED_UUID.startswith("GPU-"):
    raise RuntimeError("EXPLICIT_PHYSICAL_GPU0_UUID_REQUIRED")
if runtime.ROLE != "generator" or runtime.MODEL_DIR.name != "generator-4b":
    raise RuntimeError("ISOLATED_PINNED_4B_GENERATOR_REQUIRED")

app = runtime.app
PROOF_LOCK = threading.Lock()
PROOF = {"probe": "CONTROLLED_CUDA_ALLOCATOR_OOM", "actual_oom_count": 0,
         "fake_exception_injection": False, "physical_vram_exhaustion": False,
         "production_gpu1_access": False, "external_MAX_sends": 0,
         "instrumentation": "EXTERNAL_ISOLATED_LIFESPAN_AND_EXCEPTION_OBSERVER",
         "status": "NOT_STARTED"}
FAULT_TEXT = ""
original_lifespan = app.router.lifespan_context


def memory():
    return {"allocated_bytes": torch.cuda.memory_allocated(0),
            "reserved_bytes": torch.cuda.memory_reserved(0)}


def write_proof():
    destination = os.environ.get("KORSIK_OOM_PROOF_FILE")
    if destination:
        path = Path(destination)
        temporary = path.with_name(path.name + ".writing")
        temporary.write_text(json.dumps(PROOF, indent=2))
        temporary.replace(path)


def contains_real_cuda_oom(error):
    pending, seen = [error], set()
    while pending and len(seen) < 32:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, torch.cuda.OutOfMemoryError):
            return True
        pending.extend(getattr(item, "exceptions", ()))
        for nested in (getattr(item, "__cause__", None), getattr(item, "__context__", None)):
            if nested is not None:
                pending.append(nested)
    return False


class ObserveActualOOM:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except BaseException as error:
            if scope.get("type") == "http" and scope.get("path") == "/generate" and contains_real_cuda_oom(error):
                with PROOF_LOCK:
                    PROOF["actual_oom_count"] += 1
                    PROOF.setdefault("first_oom_at_monotonic", time.monotonic())
                    PROOF["last_oom_at_monotonic"] = time.monotonic()
                    PROOF["last_observed_exception"] = "torch.cuda.OutOfMemoryError"
                    PROOF["after_oom_memory"] = memory()
                    PROOF["status"] = "REAL_ALLOCATOR_OOM_OBSERVED_BUSINESS_CHECKS_STILL_REQUIRED"
                    write_proof()
            raise  # Preserve the unchanged application's generic500 behavior.


@asynccontextmanager
async def capped_lifespan(application):
    global FAULT_TEXT
    async with original_lifespan(application):
        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise RuntimeError("EXACTLY_ONE_EXPOSED_CUDA_DEVICE_REQUIRED")
        properties = torch.cuda.get_device_properties(0)
        actual_uuid = str(getattr(properties, "uuid", ""))
        normalize = lambda value: value.lower().removeprefix("gpu-").replace("-", "")
        if normalize(actual_uuid) != normalize(EXPECTED_UUID):
            raise RuntimeError("PHYSICAL_GPU_UUID_MISMATCH_NO_FAULT_ARMED")
        torch.cuda.synchronize(0)
        torch.cuda.empty_cache()
        baseline = memory()
        if not 8 * 1024**3 <= baseline["allocated_bytes"] < 11 * 1024**3:
            raise RuntimeError("UNEXPECTED_4B_BASELINE_NO_FAULT_ARMED")
        allowed_bytes = baseline["reserved_bytes"] + 1024 * 1024
        fraction = allowed_bytes / properties.total_memory
        if not 0 < fraction < 0.95:
            raise RuntimeError("INVALID_ALLOCATOR_LIMIT_NO_FAULT_ARMED")
        # Large but bounded synthetic prefill asks the actual decoder for real
        # activation memory, instead of assuming a tiny request exhausts gaps.
        FAULT_TEXT = "Обед проходит с 12:00 до 13:00. Синтетическое приложение: " + "x " * 3000
        tokenizer = runtime.STATE["tokenizer"]
        while len(tokenizer.encode(FAULT_TEXT, add_special_tokens=False)) > 2800:
            FAULT_TEXT = FAULT_TEXT[:-400]
        fixture_tokens = len(tokenizer.encode(FAULT_TEXT, add_special_tokens=False))
        torch.cuda.set_per_process_memory_fraction(fraction, 0)
        with PROOF_LOCK:
            PROOF.update(status="ARMED_NOT_YET_PROVEN", gpu_uuid=actual_uuid,
                         baseline_memory=baseline, allocator_limit_bytes=allowed_bytes,
                         allocator_fraction=fraction, free_headroom_bytes=1024 * 1024,
                         synthetic_evidence_tokens=fixture_tokens,
                         artifact_fingerprint=runtime.STATE["fingerprint"],
                         generation_runtime_source_sha256=runtime.sha(Path(runtime.__file__)),
                         wrapper_source_sha256=runtime.sha(Path(__file__)))
            write_proof()
        try:
            yield
        finally:
            torch.cuda.set_per_process_memory_fraction(1.0, 0)


app.router.lifespan_context = capped_lifespan
app.add_middleware(ObserveActualOOM)


@app.get("/__isolated_oom_proof/prefill-fixture", include_in_schema=False)
def prefill_fixture():
    return {"text": FAULT_TEXT, "classification": "SYNTHETIC_MEMORY_TEST_ONLY"}


@app.get("/__isolated_oom_proof/status", include_in_schema=False)
def proof_status():
    with PROOF_LOCK:
        return {**PROOF, "runtime_inference_lock_locked": runtime.LOCK.locked()}


@app.post("/__isolated_oom_proof/disarm", include_in_schema=False)
def disarm():
    if not runtime.LOCK.acquire(timeout=2):
        raise HTTPException(409, "WAIT_FOR_ISOLATED_INFERENCE_TO_FINISH")
    try:
        torch.cuda.set_per_process_memory_fraction(1.0, 0)
        torch.cuda.synchronize(0)
        torch.cuda.empty_cache()
        with PROOF_LOCK:
            PROOF.update(allocator_cap_disarmed=True, after_disarm_memory=memory(),
                         disarmed_at_monotonic=time.monotonic())
            write_proof()
    finally:
        runtime.LOCK.release()
    return {"allocator_cap_disarmed": True}

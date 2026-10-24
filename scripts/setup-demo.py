"""Create a clearly synthetic test card and validate/activate installed local profile."""

import json, time, uuid
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
from korsik.settings import get_settings
from korsik.rag_identity import pipeline_identities

s = get_settings()
c = httpx.Client(base_url="http://api:8000/api/v1", timeout=90, trust_env=False)
r = c.post(
    "/auth/login",
    json={
        "email": s.admin_email,
        "password": Path("/operator/password").read_text().strip(),
    },
)
r.raise_for_status()
actor = r.json()["user"]
c.headers.update(
    {
        "Cookie": "; ".join(f"{k}={v}" for k, v in r.cookies.items()),
        "X-CSRF-Token": r.json()["csrf_token"],
    }
)


def call(m, p, **kwargs):
    h = kwargs.pop("headers", {})
    h["Idempotency-Key"] = str(uuid.uuid4())
    r = c.request(m, p, headers=h, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"{m} {p} {r.status_code} {r.text[:700]}")
    return r


items = call("GET", "/employees").json()["items"]
employee = next(
    (e for e in items if e.get("external_employee_id") == "DEMO-KORSIK-001"), None
)
if not employee:
    employee = call(
        "POST",
        "/employees",
        json={
            "name": "Демо сотрудник (тестовый стенд)",
            "external_employee_id": "DEMO-KORSIK-001",
            "site_id": "GrPP",
            "start_date": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(),
            "employment_type": "OFFICE",
            "timezone": "Europe/Moscow",
            "assigned_hr_id": actor["id"],
            "scenario_id": "00000000-0000-4000-8000-000000000100",
        },
    ).json()
if employee.get("employment_stage") != "ACTIVE":
    episode = call("GET", f"/employment-episodes/{employee['episode_id']}").json()
    call(
        "POST",
        f"/employment-episodes/{episode['id']}/stage",
        json={
            "stage": "ACTIVE",
            "expected_episode_id": episode["id"],
            "expected_state_version": episode["version"],
            "reason": "Только вымышленная демонстрационная карточка для проверки RAG, не кадровое решение",
        },
    )
    employee = call("GET", f"/employees/{employee['id']}").json()
conv = call("POST", "/conversations", json={"employee_id": employee["id"]}).json()
models = call("GET", "/local-ai/models").json()["items"]
installed = {m["role"]: m["id"] for m in models if m.get("deployment_selected")}
profiles = call("GET", "/local-ai/profiles").json()["items"]
profile = next(
    (
        p
        for p in profiles
        if p.get("configuration", {}).get("models") == installed
        and p.get("configuration", {}).get("pipeline_identities")
        == pipeline_identities()
    ),
    None,
)
if not profile:
    profile = call(
        "POST",
        "/local-ai/profiles",
        json={
            "name": "CPU Granite/Horizon + локальная Qwen3.5 4B",
            "models": installed,
            "generation_policy": "LOCAL_GENERATION",
            "retrieval_profile": "cpu",
            "reason": "Демонстрационный on-prem профиль; offline artifacts и ограниченный guarded-pipeline implementation review проверены",
        },
    ).json()
profile = call("GET", f"/local-ai/profiles/{profile['id']}")
validation = call(
    "POST",
    f"/local-ai/profiles/{profile.json()['id']}/tests",
    headers={"If-Match": profile.headers["etag"]},
    json={},
).json()
deadline = time.monotonic() + 240
while time.monotonic() < deadline:
    value = call("GET", "/local-ai/validations/" + validation["id"]).json()
    if value["status"] not in {"QUEUED", "RUNNING"}:
        break
    time.sleep(2)
print(
    json.dumps(
        {"profile_id": profile.json()["id"], "validation": value}, ensure_ascii=False
    ),
    flush=True,
)
if value["status"] != "PASSED":
    raise RuntimeError("Local profile failed actual runtime validation")
profile = call("GET", f"/local-ai/profiles/{profile.json()['id']}")
active = call(
    "POST",
    f"/local-ai/profiles/{profile.json()['id']}/activate",
    headers={"If-Match": profile.headers["etag"]},
    json={
        "reason": "Measured local runtime + implementation semantic review; company pilot acceptance remains separate"
    },
).json()
record = {
    "employee_id": employee["id"],
    "conversation_id": conv["id"],
    "episode_id": employee["episode_id"],
    "profile_id": active["id"],
    "validation_id": value["id"],
    "data_classification": "SYNTHETIC_EMPLOYEE_FOR_DEMO",
}
Path("/data/demo-context.json").write_text(json.dumps(record, indent=2))
print(json.dumps(record), flush=True)

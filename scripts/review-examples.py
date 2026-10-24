"""Apply the recorded review of supplied example files through public HR API.
Intended for this demo release only; the policy owner must approve production use.
"""

import httpx, json, uuid, time
from pathlib import Path
from korsik.settings import get_settings

s = get_settings()
client = httpx.Client(base_url="http://api:8000/api/v1", timeout=240, trust_env=False)
r = client.post(
    "/auth/login",
    json={
        "email": s.admin_email,
        "password": Path("/operator/password").read_text().strip(),
    },
)
r.raise_for_status()
client.headers.update(
    {
        "Cookie": "; ".join(f"{k}={v}" for k, v in r.cookies.items()),
        "X-CSRF-Token": r.json()["csrf_token"],
    }
)


def call(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Idempotency-Key"] = str(uuid.uuid4())
    r = client.request(method, path, headers=headers, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:900]}")
    return r


reviews = json.loads(Path("/srv/seed/example-review.json").read_text())
records = json.loads(Path("/data/demo-import.json").read_text())
report = []
for rec in records:
    rid = rec["revision_id"]
    rule = reviews[rec["title"]]
    revision = call("GET", "/knowledge/revisions/" + rid).json()
    if revision["status"] == "PUBLISHED":
        report.append({"revision_id": rid, "status": "PUBLISHED"})
        continue
    if revision.get("sha256") != rule["source_sha256"]:
        raise RuntimeError("Review is pinned to another source file")
    reason = rule["review_scope"]
    blocks = call("GET", f"/knowledge/revisions/{rid}/extraction").json()["blocks"]
    for b in blocks:
        body = None
        if b["kind"] == "FIGURE":
            if b.get("origin") == "PPTX_SMARTART_OR_CHART":
                description = rule["diagram_descriptions"].get(str(b["page_no"]))
                if not description:
                    raise RuntimeError("Unreviewed diagram")
                body = {
                    "patch_type": "FIGURE_DESCRIPTION",
                    "description": description,
                    "reason": "Сверены точные подписи SmartArt по XML оригинала и изображению слайда. "
                    + reason,
                }
            elif b.get("disposition") != "REFERENCE_ONLY":
                body = {
                    "patch_type": "DISPOSITION",
                    "disposition": "REFERENCE_ONLY",
                    "reason": rule["other_figures"] + " " + reason,
                }
        elif b["kind"] == "TABLE":
            body = {
                "patch_type": "TABLE",
                "grid": b["grid"],
                "reason": "Проверена таблица исходной презентации. " + reason,
            }
        if body:
            ext = call("GET", f"/knowledge/revisions/{rid}/extraction")
            body["expected_extraction_revision_id"] = ext.json()[
                "extraction_revision_id"
            ]
            call(
                "PATCH",
                f"/knowledge/revisions/{rid}/extraction/blocks/{b['id']}",
                json=body,
                headers={"If-Match": ext.headers["etag"]},
            )
    pages = call("GET", f"/knowledge/revisions/{rid}/pages").json()["items"]
    for page in pages:
        ext = call("GET", f"/knowledge/revisions/{rid}/extraction")
        call(
            "PATCH",
            f"/knowledge/revisions/{rid}/pages/{page['page_no']}/review",
            headers={"If-Match": ext.headers["etag"]},
            json={
                "action": "REVIEW_CONTENT",
                "reason": "Текст и значимые схемы сверены с предоставленным оригиналом и предпросмотром. "
                + reason,
                "expected_extraction_revision_id": ext.json()["extraction_revision_id"],
            },
        )
    for action, body in [
        ("candidate-build", {}),
        ("qa-runs", {}),
        ("review", {"action": "APPROVE", "reason": reason}),
        ("publish", {"reason": reason}),
    ]:
        revision = call("GET", "/knowledge/revisions/" + rid)
        result = call(
            "POST",
            f"/knowledge/revisions/{rid}/{action}",
            headers={"If-Match": revision.headers["etag"]},
            json=body,
        )
        if action in {"candidate-build", "qa-runs"}:
            operation = result.json()
            deadline = time.monotonic() + 900
            while (
                operation["status"] in {"PENDING", "RUNNING"}
                and time.monotonic() < deadline
            ):
                time.sleep(2)
                operation = call("GET", "/operations/" + operation["id"]).json()
            if operation["status"] != "SUCCEEDED" or (
                action == "qa-runs"
                and any(
                    q["status"] != "PASSED"
                    for q in operation.get(
                        "qa_results", operation.get("result", {}).get("qa_results", [])
                    )
                )
            ):
                raise RuntimeError(
                    f"{action} did not succeed: "
                    + json.dumps(operation, ensure_ascii=False)[:1000]
                )
        print(
            json.dumps(
                {
                    "document": rec["title"],
                    "action": action,
                    "http": result.status_code,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    report.append(
        {
            "revision_id": rid,
            "status": call("GET", "/knowledge/revisions/" + rid).json()["status"],
            "source_sha256": rule["source_sha256"],
            "review_scope": reason,
        }
    )
Path("/data/demo-publication-report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2)
)

"""Exercise real MAX image/file allocation and upload without sending a message."""

import asyncio
from io import BytesIO
import json
from pathlib import Path
import httpx
from PIL import Image
from korsik.max_connector import API, token, safe_cdn_url


async def main():
    rows = []
    image = BytesIO()
    Image.new("RGB", (512, 512), "#175447").save(image, "PNG")
    fixtures = [
        ("image", "synthetic.png", "image/png", image.getvalue()),
        (
            "file",
            "synthetic.txt",
            "text/plain",
            "КОРСИК: синтетический файл проверки. Не регламент компании.".encode(),
        ),
    ]
    async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
        for kind, name, content_type, data in fixtures:
            row = {"kind": kind, "synthetic_fixture": True}
            try:
                allocated = await client.post(
                    API + "/uploads",
                    params={"type": kind},
                    headers={"Authorization": token()},
                )
                row["allocation_http"] = allocated.status_code
                allocated.raise_for_status()
                value = allocated.json()
                url = value.get("url", "")
                row["cdn_allowed"] = await asyncio.to_thread(safe_cdn_url, url)
                if not row["cdn_allowed"]:
                    raise ValueError("upload_host_not_allowed")
                uploaded = await client.post(
                    url, files={"data": (name, data, content_type)}
                )
                row["upload_http"] = uploaded.status_code
                uploaded.raise_for_status()
                result = uploaded.json()
                row["adapter_payload_available"] = bool(
                    result.get("photos")
                    if kind == "image"
                    else result.get("token") or value.get("token")
                )
                row["status"] = "PASS" if row["adapter_payload_available"] else "FAIL"
            except Exception as exc:
                row.update(status="FAIL", error_type=type(exc).__name__)
            rows.append(row)
    report = {
        "schema": "korsik-real-max-upload-v1",
        "status": "PASS" if all(r["status"] == "PASS" for r in rows) else "FAIL",
        "messages_sent": 0,
        "recipient_ids_used": 0,
        "scope": "Actual allocation/upload transport only; not chat rendering or employee E2E",
        "checks": rows,
    }
    Path("/operator/max-upload-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    return report["status"] == "PASS"


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)

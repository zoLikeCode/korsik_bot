"""Real warmed single-caller latency, not a semantic-quality golden benchmark.

Run inside the app network with the synthetic employee from setup-demo.py.
No persisted conversations, outbox or MAX messages are created.
"""

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

from korsik import retrieval
from korsik.db import SessionLocal, new_id
from korsik.models import Entity

BASE = [
    "Как передавать пароли к конфиденциальной информации?",
    "Нужно ли соблюдать коммерческую тайну после увольнения?",
    "Кто разрешает посетителю фотографировать конфиденциальные документы?",
    "Когда сотрудник подписывает антикоррупционное обязательство?",
    "Можно ли дарить деловому партнёру денежный подарок?",
    "Какова миссия компании ПОЛИПЛАСТИК?",
    "Как поступать при выявлении коррупционного нарушения?",
    "Какие основные цели политики в области качества и экологии?",
    "Какие обязанности сотрудника по сохранению коммерческой тайны?",
    "Кто должен соблюдать антикоррупционную политику компании?",
]
PREFIX = [
    "",
    "Подскажи, пожалуйста: ",
    "Хочу уточнить: ",
    "Объясни правило: ",
    "Что сказано в документах: ",
    "Помоги разобраться: ",
    "Нужен ответ по внутренним документам: ",
    "Уточни для сотрудника: ",
    "Мой вопрос к базе знаний: ",
    "Кратко поясни: ",
]


def stats(values):
    ordered = sorted(values)
    return (
        {
            "count": len(ordered),
            "p50_seconds": round(ordered[math.ceil(len(ordered) * 0.5) - 1], 4),
            "p95_seconds": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 4),
            "max_seconds": round(max(ordered), 4),
        }
        if ordered
        else None
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--employee-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=["FULL", "CORE"], default="FULL")
    args = parser.parse_args()
    retrieval.MODE = args.mode
    original_request = retrieval._request
    current = defaultdict(float)

    def measured(url, payload=None, method="POST", timeout=30):
        stage = (
            "generation_request"
            if "/generate" in url
            else (
                "encode_request"
                if "/embed" in url
                else "rerank_request" if "/rerank" in url else "vector_request"
            )
        )
        started = time.monotonic()
        try:
            result = original_request(url, payload, method, timeout)
            if stage == "generation_request":
                for key in ("queue_seconds", "inference_seconds"):
                    if key in result:
                        current[key] += result[key]
            return result
        finally:
            current[stage] += time.monotonic() - started

    retrieval._request = measured
    rows = []
    with SessionLocal() as db:
        employee = db.get(Entity, args.employee_id)
        if not employee or employee.kind != "employee":
            raise ValueError("synthetic_employee_required")
        conv = Entity(
            id=new_id(), kind="conversation", data={"employee_id": employee.id}
        )
        if len(retrieval.published_corpus(db, employee)) < 3:
            raise ValueError("three_published_examples_required")
        retrieval.answer_question(db, conv, BASE[0])  # separate warmup
        for prefix in PREFIX:
            for question in BASE:
                current.clear()
                started = time.monotonic()
                try:
                    answer = retrieval.answer_question(db, conv, prefix + question)
                    row = {
                        "mode": answer["mode"],
                        "reason": answer.get("reason"),
                        "citations": len(answer.get("citations", [])),
                    }
                except Exception as exc:
                    row = {"mode": "ERROR", "error_type": type(exc).__name__}
                row.update(
                    id=len(rows) + 1,
                    seconds=round(time.monotonic() - started, 4),
                    stages={k: round(v, 4) for k, v in current.items()},
                )
                rows.append(row)
                if len(rows) % 10 == 0:
                    print(
                        json.dumps({"completed": len(rows), "mode": args.mode}),
                        flush=True,
                    )
        db.rollback()
    report = {
        "schema": "korsik-latency-v1",
        "profile": "100 distinct bounded Russian questions (10 topics × 10 prompt variants), warmed, single active caller, no MAX send",
        "quality_limit": "Timing workload, not 100 independent semantic gold cases or company acceptance",
        "runtime_mode": args.mode,
        "total": stats([r["seconds"] for r in rows]),
        "modes": dict(Counter(r["mode"] for r in rows)),
        "stages": {
            key: stats([r["stages"][key] for r in rows if key in r["stages"]])
            for key in sorted({k for r in rows for k in r["stages"]})
        },
        "target_p95_seconds": 30,
        "rows": rows,
    }
    report["latency_target_met"] = report["total"]["p95_seconds"] <= 30 and not report[
        "modes"
    ].get("ERROR")
    report["generated"] = stats(
        [r["seconds"] for r in rows if r["mode"] == "LOCAL_GENERATED"]
    )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

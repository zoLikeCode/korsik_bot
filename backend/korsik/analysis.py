"""Local CPU feedback baseline. No pretend prediction before labeled training."""
import collections
import json
import math
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from .auth import get_actor
from .common import assert_employee, audit, check_etag, commit, get_entity, save_entity, serialize, etag
from .db import get_db, now
from .models import Entity
from .parsing import digest


def feedback_terms(text):
    words = re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))
    # Negations and contrast conjunctions must survive sentiment tokenization.
    return words + [words[i] + "_" + words[i + 1] for i in range(len(words) - 1)]

router = APIRouter()
ARTIFACT = Path(os.environ.get("FEEDBACK_MODEL_PATH", "/models/feedback/model.json"))
LABELS = {"NEGATIVE", "NEUTRAL", "POSITIVE", "MIXED", "UNCLEAR"}


def train_model(examples, output_path, synthetic=False, validation_examples=None):
    """Fit local TF-IDF + one-vs-rest logistic regression, export plain JSON.

    Synthetic fixtures are explicitly tagged and never count as production
    evaluation. The artifact contains no source texts or personal data.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    valid = [(str(e["text"]), str(e["label"]).upper()) for e in examples if str(e.get("label", "")).upper() in LABELS]
    counts = collections.Counter(label for _, label in valid)
    if len(counts) < 2 or min(counts.values()) < 5:
        raise ValueError("At least two labeled classes with five examples each are required")
    vectorizer = TfidfVectorizer(tokenizer=feedback_terms, token_pattern=None, lowercase=False, max_features=30000, sublinear_tf=True, norm="l2")
    features = vectorizer.fit_transform([text for text, _ in valid])
    labels = [label for _, label in valid]
    weights, intercepts = {}, {}
    for label in sorted(counts):
        clf = LogisticRegression(C=2.0, max_iter=1000, random_state=42, class_weight="balanced")
        clf.fit(features, [int(value == label) for value in labels])
        weights[label] = clf.coef_[0].tolist()
        intercepts[label] = float(clf.intercept_[0])
    data = {"schema": "korsik-feedback-tfidf-logistic-ovr-v1", "trained_at": now().isoformat(),
            "labels": sorted(counts), "class_counts": dict(counts), "vocabulary": {word: int(index) for word, index in vectorizer.vocabulary_.items()},
            "idf": vectorizer.idf_.tolist(), "weights": weights, "intercepts": intercepts,
            "training_digest": digest(valid), "training_data_kind": "SYNTHETIC_FIXTURES" if synthetic else "OPERATOR_LABELED_DATA",
            "evaluation_status": "SYNTHETIC_ONLY" if synthetic else "REQUIRES_IN_DOMAIN_EVALUATION", "ready_for_auto_action": False}
    if validation_examples:
        from sklearn.metrics import accuracy_score, f1_score
        transformed = vectorizer.transform([e["text"] for e in validation_examples])
        import numpy as np
        predictions = np.stack([transformed @ np.array(weights[label]) + intercepts[label] for label in data["labels"]], axis=1).argmax(axis=1)
        predicted = [data["labels"][i] for i in predictions]
        gold = [str(e["label"]).upper() for e in validation_examples]
        data["held_out_metrics"] = {"examples": len(gold), "accuracy": accuracy_score(gold, predicted),
                "macro_f1": f1_score(gold, predicted, average="macro", zero_division=0), "data_kind": "SYNTHETIC" if synthetic else "OPERATOR_PROVIDED"}
    data["fingerprint"] = digest(data)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def analyze_text(text):
    pending = {"status": "PENDING_HR_REVIEW", "sentiment": None, "topics": [], "risk": None,
               "decision_authority": "HUMAN", "raw_text_sent_to_external_service": False}
    if not text.strip():
        return {**pending, "reason": "EMPTY_FEEDBACK"}
    if not ARTIFACT.exists():
        return {**pending, "reason": "NO_TRAINED_LOCAL_CLASSIFIER"}
    try:
        data = json.loads(ARTIFACT.read_text())
        expected = data.pop("fingerprint")
        if digest(data) != expected:
            return {**pending, "reason": "MODEL_INTEGRITY_ERROR"}
        tf = collections.Counter(feedback_terms(text))
        features = {}
        for word, count in tf.items():
            index = data["vocabulary"].get(word)
            if index is not None:
                features[index] = (1 + math.log(count)) * data["idf"][index]
        norm = math.sqrt(sum(value * value for value in features.values())) or 1
        features = {index: value / norm for index, value in features.items()}
        scores = {label: data["intercepts"][label] + sum(value * data["weights"][label][index] for index, value in features.items()) for label in data["labels"]}
        ranked = sorted(scores, key=scores.get, reverse=True)
        margin = scores[ranked[0]] - scores[ranked[1]]
        return {**pending, "suggested_sentiment": ranked[0], "margin": round(margin, 5),
                "model_fingerprint": expected, "reason": "MODEL_REQUIRES_HUMAN_REVIEW",
                "evaluation_status": data.get("evaluation_status", "NOT_EVALUATED")}
    except Exception:
        return {**pending, "reason": "LOCAL_CLASSIFIER_UNAVAILABLE"}


def analyze_feedback(db, feedback_entity):
    text = feedback_entity.data.get("text") or feedback_entity.data.get("answer") or ""
    result = analyze_text(str(text))
    return save_entity(db, "feedback_analysis", {**result, "feedback_id": feedback_entity.id,
        "employee_id": feedback_entity.data.get("employee_id")}, status="PENDING_HR_REVIEW", parent_id=feedback_entity.id)


def process_analysis_job(db, job):
    if job.kind not in {"FEEDBACK_ANALYSIS", "ANALYZE_FEEDBACK"}:
        return False
    feedback = get_entity(db, job.payload.get("feedback_id") or job.payload.get("entity_id"))
    existing = db.scalar(select(Entity).where(Entity.kind == "feedback_analysis", Entity.parent_id == feedback.id))
    if not existing:
        analyze_feedback(db, feedback)
        db.commit()
    return True


@router.get("/feedback-analyses")
def list_analyses(actor=Depends(get_actor), db=Depends(get_db)):
    rows = db.scalars(select(Entity).where(Entity.kind == "feedback_analysis")).all()
    result = []
    for row in rows:
        if actor.role not in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
            continue
        employee_id = row.data.get("employee_id")
        if employee_id and actor.role != "PLATFORM_ADMIN":
            try:
                assert_employee(db, actor, employee_id)
            except HTTPException:
                continue
        result.append(serialize(row))
    return {"items": result, "next_cursor": None}


@router.get("/feedback-analyses/{analysis_id}")
def get_analysis(analysis_id: str, response: Response, actor=Depends(get_actor), db=Depends(get_db)):
    row = get_entity(db, analysis_id, "feedback_analysis")
    if actor.role not in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
        raise HTTPException(404, "analysis_not_found")
    if row.data.get("employee_id") and actor.role != "PLATFORM_ADMIN":
        assert_employee(db, actor, row.data["employee_id"])
    response.headers["ETag"] = etag(row)
    result = serialize(row)
    # Resolve the source at read time; classification artifacts never duplicate
    # personal feedback. A corrupt/cross-employee link cannot widen HR access.
    feedback_id = row.data.get("feedback_id") or row.parent_id
    feedback = db.get(Entity, feedback_id) if feedback_id else None
    if feedback:
        db.refresh(feedback)
        employee_id = feedback.data.get("employee_id")
        if feedback.kind != "feedback" or not employee_id or employee_id != row.data.get("employee_id"):
            raise HTTPException(404, "analysis_source_not_found")
        assert_employee(db, actor, employee_id)
        result["source_feedback"] = {"id": feedback.id, "version": feedback.version,
            "text": str(feedback.data.get("text") or feedback.data.get("answer") or ""),
            "question_id": feedback.data.get("question_id"), "survey_run_id": feedback.data.get("survey_run_id"),
            "question_prompt": feedback.data.get("question_prompt"), "answer_revision": feedback.data.get("answer_revision"),
            "episode_id": feedback.data.get("episode_id"),
            "created_at": feedback.created_at.isoformat()}
        result["source_text"] = result["source_feedback"]["text"]
        result["source_status"] = "AVAILABLE"
    else:
        result["source_feedback"] = None
        result["source_status"] = "MISSING"
    return result


@router.post("/feedback-analyses/{analysis_id}/review")
def review_analysis(analysis_id: str, body: dict, request: Request, response: Response, actor=Depends(get_actor), db=Depends(get_db)):
    row = get_entity(db, analysis_id, "feedback_analysis", lock=True)
    db.refresh(row, with_for_update=True)
    get_analysis(analysis_id, Response(), actor, db)
    check_etag(request, row)
    label = body.get("sentiment") or body.get("label")
    topics = body.get("topics", [])
    if not isinstance(topics, list) or len(topics)>20 or any(not isinstance(value, str) or not value.strip() or len(value)>120 for value in topics):
        raise HTTPException(422, "invalid_feedback_topics")
    if not isinstance(label, str) or label not in LABELS or not isinstance(body.get("reason"), str) or len(body["reason"].strip()) < 3:
        raise HTTPException(422, "sentiment_and_review_reason_required")
    stamp = now().isoformat()
    review = save_entity(db, "feedback_analysis_review", {"analysis_id": row.id,
        "previous_sentiment": row.data.get("sentiment"), "new_sentiment": label,
        "previous_topics": row.data.get("topics", []), "new_topics": topics,
        "reason": str(body["reason"])[:2000], "actor_id": actor.id, "reviewed_at": stamp},
        status="IMMUTABLE", parent_id=row.id, owner_id=actor.id)
    db.flush()
    row.data = {**row.data, "sentiment": label, "reviewed_by": actor.id, "reviewed_at": stamp,
                "review_history_ids": row.data.get("review_history_ids", []) + [review.id],
                "review_reason": str(body["reason"])[:2000], "topics": topics, "decision_authority": "HUMAN"}
    row.status = "REVIEWED"
    audit(db, actor, "feedback.analysis.reviewed", row.id, {"sentiment": label, "review_id": review.id}, reason=body["reason"])
    commit(db)
    response.headers["ETag"] = etag(row)
    return serialize(row)


@router.get("/feedback-analyses/{analysis_id}/reviews")
def analysis_reviews(analysis_id: str, actor=Depends(get_actor), db=Depends(get_db)):
    get_analysis(analysis_id, Response(), actor, db)
    rows = db.scalars(select(Entity).where(Entity.kind == "feedback_analysis_review", Entity.parent_id == analysis_id).order_by(Entity.created_at)).all()
    return {"items": [serialize(row) for row in rows], "total": len(rows), "next_cursor": None}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Train a local feedback TF-IDF/logistic model; no runtime network")
    parser.add_argument("command", choices=["train"])
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--synthetic", action="store_true")
    args = parser.parse_args()
    rows = [json.loads(line) for line in Path(args.dataset).read_text().splitlines() if line.strip()]
    training = [row for row in rows if row.get("split", "train") == "train"]
    heldout = [row for row in rows if row.get("split") == "test"]
    artifact = train_model(training, args.output, synthetic=args.synthetic, validation_examples=heldout)
    print(json.dumps({"fingerprint": artifact["fingerprint"], "evaluation_status": artifact["evaluation_status"], "held_out_metrics": artifact.get("held_out_metrics")}, ensure_ascii=False))

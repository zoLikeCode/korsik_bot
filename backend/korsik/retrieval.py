"""Published-only scoped hybrid retrieval and grounded local answer rendering."""
from __future__ import annotations
import collections
import hashlib
import math
import os
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from functools import lru_cache
from contextlib import contextmanager

import httpx
from sqlalchemy import select

from .models import Entity, Message
from .db import new_id
from .parsing import digest
from .rag_identity import pipeline_identities, generation_payload_fingerprint
from .organization_scope import current_employee, employee_data, employee_session, org_unit_ancestors

COLLECTION = "korsik_granite97_r2_v1"
EMBEDDER = os.environ.get("EMBEDDER_URL", os.environ.get("KORSIK_ENCODER_URL", "http://embedder:8000")).rstrip("/")
RERANKER = os.environ.get("RERANKER_URL", os.environ.get("KORSIK_RERANKER_URL", "http://reranker:8000")).rstrip("/")
GENERATOR = os.environ.get("GENERATOR_URL", os.environ.get("KORSIK_GENERATOR_URL", "http://generator:8000")).rstrip("/")
QDRANT = os.environ.get("QDRANT_URL", os.environ.get("KORSIK_QDRANT_URL", "http://qdrant:6333")).rstrip("/")
MODE = os.environ.get("KORSIK_AI_MODE", os.environ.get("KORSIK_PRODUCT_MODE", "FULL")).upper()
STOP = set("а и в во на с со к по у о об от до за для из или это как где мне я вы мы он она они что ли же не да нет будет быть который какая какие когда".split())


def terms(text):
    return [t for t in re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е")) if len(t) > 2 and t not in STOP]


@lru_cache(maxsize=50000)
def _lexical_stem(word):
    if not re.search(r"[а-я]", word):
        return word
    import snowballstemmer
    # Stemmer instances have mutable cursors. Use an instance per cache miss so
    # concurrent API threads never share that state; no downloaded dictionaries.
    return snowballstemmer.stemmer("russian").stemWord(word)


def lexical_terms(text):
    return [_lexical_stem(word.casefold()) for word in re.findall(r"[a-zA-Zа-яА-Я0-9]+", text.replace("ё", "е").replace("Ё", "Е"))
            if (len(word) > 2 or word.isdecimal() or len(word) > 1 and word.isupper()) and word.casefold() not in STOP]



def evidence_topic_supported(question, evidence):
    """A retrieved neighbour is not proof that the requested topic is covered.

    This conservative absence check uses source text, not ranking scores or
    document titles. It is not an entailment check; nonzero overlap still needs
    the existing completeness, citation and normative guards.
    """
    fillers = set(lexical_terms("можно пожалуйста подскажите расскажите объясните узнать хочу нужно необходимо должен компания общество правило вопрос ответ информация документ какой кто куда откуда зачем почему такой это"))
    query, _ = query_terms_with_equivalents(question)
    focus = set(lexical_terms(query)) - fillers
    canonical_text = " ".join(item["text"] for item in evidence)
    source = set(lexical_terms(canonical_text))
    # A generic access rule cannot establish access to an explicitly named
    # system. Require the requested identifiers in current canonical evidence;
    # punctuation/case variants such as Wi-Fi/WiFi describe the same token.
    # These are syntax-derived identifiers, never a topic/vendor allowlist.
    def identifiers(text):
        return {re.sub(r"[^a-z0-9]", "", value) for value in
                re.findall(r"(?<![a-z0-9])[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*(?![a-z0-9])", text.casefold())}
    named = identifiers(question) - {"a", "an", "and", "are", "can", "do", "does", "for", "how", "i", "in", "is", "it", "of", "on", "or", "please", "the", "to", "what", "when", "where", "which", "who"}
    return bool(focus & source) and named <= identifiers(canonical_text)


def model_abstained(text):
    """Recognise explicit insufficiency reports, not ordinary policy negation."""
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    return bool(re.search(
        r"(?:недостаточно\s+(?:данных|информации|сведений|контекста)|"
        r"(?:в\s+)?(?:(?:предоставленных|доступных|найденных|указанных)\s+)?(?:источниках|документах|материалах|данных).{0,100}(?:нет\s+(?:информации|данных|сведений)|не\s+(?:указано|содержится|описано|найдено))|"
        r"(?:не\s+могу|невозможно)\s+(?:дать\s+)?(?:ответить|подтвердить|определить).{0,100}(?:источник|документ|данн|информац|контекст)|"
        r"(?:источники|документы).{0,50}не\s+содержат\s+(?:информации|данных|сведений)|"
        r"(?:insufficient|not enough)\s+(?:evidence|information|data)|"
        r"(?:provided|available)\s+(?:documents|sources|evidence).{0,80}(?:do not|does not|don't|doesn't)\s+(?:contain|provide|mention))", normalized))


def heading_topic_signal(question, parent_text, document_title=""):
    """A short native heading can identify a requested list's subject.

    This is a bounded ranking feature, never evidence or an access grant. It
    helps a specific policy heading compete with generic overview diagrams.
    """
    fillers = {"что", "как", "каки", "каков", "услов", "требован", "вид", "категор", "перечен", "состав", "полн", "счита", "относ", "вход", "включа", "расскаж", "посмотрет", "компан", "обществ", "основн", "пожалуйст", "подскаж", "хоч", "уточн", "объясн", "правил"}
    query = set(lexical_terms(question)) - fillers - set(lexical_terms(document_title))
    heading = next((line.strip() for line in parent_text.splitlines() if line.strip()), "")[:180]
    words = set(lexical_terms(heading)) - fillers
    if not words:
        return 0.0
    def matches(word):
        return any(word == term or (min(len(word), len(term)) >= 5 and (word.startswith(term) or term.startswith(word)) and min(len(word), len(term))/max(len(word), len(term)) >= .8) for term in query)
    return sum(matches(word) for word in words) / len(words)


def topic_alignment(question, text, document_title=""):
    """Query focus coverage; repeated document-title words do not decide intent."""
    fillers = {"кто", "как", "каки", "каков", "основн", "нужн", "необходим", "компан", "обществ", "пожалуйст", "подскаж", "хоч", "уточн", "объясн", "правил", "расскаж", "знат", "полн", "следова"}
    query = set(lexical_terms(query_terms_with_equivalents(question)[0])) - fillers - set(lexical_terms(document_title))
    source = set(lexical_terms(text))
    return len(query & source) / max(1, len(query))


def audience_scope_question(question):
    """Document applicability differs from who performs one particular action."""
    text = question.casefold().replace("ё", "е")
    document = r"(?:политик|положени|регламент|кодекс|инструкци|стандарт|правил|требовани|норм)[а-я]*"
    explicit_scope = r"(?:на\s+кого\s+(?:распространя|действу)|к\s+кому\s+применя|кого\s+каса|для\s+кого\s+(?:предназнач|обязател)|область\s+применения|круг\s+(?:лиц|участников)|аудитори[а-я]*)"
    compliance = r"(?:\bкто\b|какие\s+(?:лица|работники|сотрудники|участники)).{0,50}(?:соблюда[а-я]*\s+|(?:выполня|исполня)[а-я]*\s+требования\s+)(?:[а-я]+\s+){0,3}" + document
    return bool(re.search(explicit_scope, text) or re.search(compliance, text))


def audience_scope_signal(question, text, document_title=""):
    """A topical applicability clause is independent of a single worker duty.

    Only source structure supplies the scope, never a generated actor list.
    No page, policy name or company role is special-cased here.
    """
    if not audience_scope_question(question):
        return 0.0
    fillers = {"кто", "как", "каки", "кого", "кому", "должен", "должн", "обяза", "соблюда", "выполня", "исполня", "требован", "распространя", "действ", "применя", "предназнач", "обязательн", "касайт", "област", "применен", "круг", "лиц", "участник", "аудитор", "политик", "положен", "регламент", "кодекс", "инструкц", "стандарт", "правил", "норм", "компан", "обществ", "настоящ", "дан", "эт", "указан", "пожалуйст", "подскаж", "уточн", "объясн", "хоч"}
    focus = set(lexical_terms(question)) - fillers
    if focus and not (focus & set(lexical_terms(document_title + " " + text))):
        return 0.0
    normalized = re.sub(r"\s+", " ", text.casefold())
    direct = re.search(r"\b(?:действие|требовани[а-я]*|положени[а-я]*|норм[а-я]*|политик[а-я]*|регламент[а-я]*|кодекс[а-я]*|инструкци[а-я]*|стандарт[а-я]*)\b.{0,180}(?:распространя[а-я]*\s+на|применя[а-я]*\s+к|обязательн[а-я]*\s+для|предназнач[а-я]*\s+для)", normalized)
    coordinated = re.search(r"\b(?:организаци[а-я]*|координаци[а-я]*|регулировани[а-я]*)\b.{0,80}\bдеятельност[а-я]*\b(.{0,650})", normalized)
    return float(bool(direct or coordinated and coordinated.group(1).count(",") >= 2))


def definition_question(question):
    return bool(re.search(r"(?:что\s+(?:такое|означает|считается|является|относится)|как\s+(?:определяется|понимается)|определени[ея])", question.casefold()))


def definition_signal(question, text):
    if not definition_question(question) or not re.search(r"(?:[—–-]\s*это\b|\bэто\s+(?:сведения|информация|совокупность)|\bпонимается\b|\bозначает\b)", text.casefold()):
        return 0.0
    focus = set(lexical_terms(question)) - {"что", "так", "означа", "счита", "явля", "относ", "как", "определя", "понима", "определен", "компан", "обществ", "пожалуйст", "подскаж", "хоч", "уточн", "объясн", "правил"}
    return float(bool(focus) and len(focus & set(lexical_terms(text))) / len(focus) >= .5)


def section_heading(text):
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return " ".join(lexical_terms(first)) if len(first) <= 180 and not re.search(r"[.!?]$", first) else ""


def sparse(text):
    counts = collections.Counter(terms(text))
    if not counts:
        return {"indices": [], "values": []}
    # Stable lexical token IDs; never Python's randomized hash(). 32-bit collisions
    # are possible and are not treated as evidence of factual correctness.
    result = collections.defaultdict(float)
    length = sum(counts.values())
    for term, tf in counts.items():
        index = int.from_bytes(hashlib.blake2s(term.encode(), digest_size=4).digest(), "big")
        result[index] += tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / 120))
    return {"indices": sorted(result), "values": [result[i] for i in sorted(result)]}


def _request(url, payload=None, method="POST", timeout=30):
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.request(method, url, json=payload)
        response.raise_for_status()
        return response.json()


@contextmanager
def _timed_stage(timings, key):
    started = time.monotonic()
    try:
        yield
    finally:
        timings[key] = round((timings.get(key) or 0) + time.monotonic() - started, 6)


def selected_model_fingerprints(db):
    try:
        from .local_ai import active_profile
    except ImportError:
        return {}
    profile = active_profile(db)
    if not profile:
        return {}
    result = {}
    for role, artifact_id in profile.data.get("configuration", {}).get("models", {}).items():
        artifact = db.get(Entity, artifact_id)
        if artifact and artifact.kind == "model_artifact":
            result[role] = artifact.data.get("fingerprint")
    return result


def scope_matches(scope, employee, db=None):
    """Only current structured server-side employee attributes grant access."""
    db = employee_session(employee, db)
    employee = current_employee(db, employee)
    return _scope_matches_current(scope, employee, db=db)


def _scope_matches_current(scope, employee, db=None, org_units=None):
    """Evaluate a freshly loaded profile, with an optional DB-derived path.

    The precomputed path is private and used only within one corpus read. It
    avoids querying the same organization tree once per document; no caller or
    indexed metadata can supply it to the public scope matcher.
    """
    scope = scope or {}
    data = employee_data(employee)
    if not data or data.get("enabled", True) is False:
        return False
    if scope.get("org_unit_ids"):
        current_units = org_unit_ancestors(db, employee) if org_units is None else org_units
        if not set(scope["org_unit_ids"]).intersection(current_units):
            return False
    checks = [("site_ids", "site_id"), ("legal_entity_ids", "legal_entity_id"),
              ("employment_types", "employment_type"), ("employment_stages", "employment_stage")]
    for plural, singular in checks:
        allowed = scope.get(plural)
        if plural == "employment_stages" and allowed is None:
            allowed = ["ACTIVE"]
        if allowed and data.get(singular) not in allowed:
            return False
    if scope.get("employee_ids") and data.get("employee_id", getattr(employee, "id", None)) not in scope["employee_ids"]:
        return False
    if scope.get("legal_entity_id") and data.get("legal_entity_id") != scope["legal_entity_id"]:
        return False
    if scope.get("audience_roles") and "EMPLOYEE" not in scope["audience_roles"]:
        return False
    if scope.get("sensitivity") in {"HR_ONLY", "CONFIDENTIAL_HR", "HR_RESTRICTED", "COMPLIANCE_RESTRICTED"}:
        return False
    return True


def is_effective(data, at=None):
    at = at or datetime.now(timezone.utc)
    for name, lower in [("effective_from", True), ("effective_to", False)]:
        value = data.get(name)
        if value:
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, TypeError, AttributeError):
                return False
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if lower and stamp > at or not lower and stamp <= at:
                return False
    return True


def published_corpus(db, employee):
    employee = current_employee(db, employee)
    if not employee_data(employee) or employee_data(employee).get("enabled", True) is False:
        return {}
    docs = db.scalars(select(Entity).where(Entity.kind == "document", Entity.status == "ACTIVE").execution_options(populate_existing=True)).all()
    org_units = org_unit_ancestors(db, employee) if any((doc.data.get("scope") or {}).get("org_unit_ids") for doc in docs) else set()
    applicable_docs = [doc for doc in docs if not doc.data.get("source_revoked")
                       and _scope_matches_current(doc.data.get("scope"), employee, org_units=org_units)]
    revision_ids = {doc.data.get("active_revision_id") for doc in applicable_docs} - {None}
    revisions = {revision.id: revision for revision in db.scalars(select(Entity).where(Entity.kind == "document_revision", Entity.id.in_(revision_ids), Entity.status == "PUBLISHED").execution_options(populate_existing=True)).all()} if revision_ids else {}
    permitted = {}
    for doc in applicable_docs:
        revision_id = doc.data.get("active_revision_id")
        if not revision_id:
            continue
        revision = revisions.get(revision_id)
        if not revision or revision.status != "PUBLISHED" or not is_effective(revision.data):
            continue
        permitted[revision.id] = (doc, revision)
    return permitted


def _local_chunks(text):
    """CORE-only lexical chunks, never represented as transformer token counts."""
    result = []
    for start in range(0, len(text), 500):
        result.append({"text": text[start:start + 600], "char_start": start, "char_end": min(len(text), start + 600)})
    return result


def make_chunks(db, revision, extraction, *, persist=True, checkpoint=None):
    from .common import save_entity
    chunks = []
    document = db.scalar(select(Entity).where(Entity.id == revision.parent_id, Entity.kind == "document").execution_options(populate_existing=True))
    # Informational index metadata only. Every search is first fenced by the
    # current document scope and current organization membership in Postgres.
    org_unit_ids = list((document.data.get("scope") or {}).get("org_unit_ids") or []) if document else []
    for source_block in extraction.get("blocks", []):
        if checkpoint:
            checkpoint()
        if source_block.get("disposition") in {"EXCLUDE_NONCONTENT", "REFERENCE_ONLY"}:
            continue
        text = source_block.get("text", "").strip()
        if source_block["kind"] == "FIGURE":
            text = source_block.get("description", "").strip()
        if not text:
            continue
        try:
            pieces = _request(RERANKER + "/chunks", {"text": text, "max_tokens": 220})["chunks"]
            joint_pieces = []
            for piece in pieces:
                children = _request(EMBEDDER + "/chunks", {"text": piece["text"], "max_tokens": 500})["chunks"]
                for child in children:
                    joint_pieces.append({**child, "char_start": piece["char_start"] + child["char_start"],
                                         "char_end": piece["char_start"] + child["char_end"]})
            pieces = joint_pieces
        except Exception:
            if MODE != "CORE":
                raise RuntimeError("local_chunk_tokenizer_unavailable")
            pieces = _local_chunks(text)
        for piece in pieces:
            parent_blocks = [b for b in extraction.get("blocks", []) if b["page_no"] == source_block["page_no"]
                             and b["kind"] in {"TEXT", "TABLE"} and b.get("disposition") not in {"REFERENCE_ONLY", "EXCLUDE_NONCONTENT"} and b.get("text", "").strip()]
            has_table = source_block["kind"] != "FIGURE" and any(b["kind"] == "TABLE" for b in parent_blocks)
            parent_ranges = [{"block_id": b["id"], "char_start": 0, "char_end": len(b["text"]), "text": b["text"], "sha256": digest(b["text"].encode())} for b in parent_blocks] if has_table else None
            parent_text = "\n\n".join(b["text"] for b in parent_blocks) if has_table else text
            content = {**piece, "revision_id": revision.id, "document_id": revision.parent_id, "org_unit_ids": org_unit_ids,
                       "chunker_version": pipeline_identities()["chunker_version"], "chunker_fingerprint": pipeline_identities()["chunker_fingerprint"],
                       "block_id": source_block["id"], "page_no": source_block["page_no"],
                       "bbox": source_block.get("bbox"), "kind": source_block["kind"], "image_sha256": source_block.get("image_sha256"),
                       "parent_text": parent_text, "parent_ranges": parent_ranges, "block_text_sha256": digest(text.encode()), "extraction_revision_id": extraction["id"],
                       "content_fingerprint": revision.data["content_fingerprint"]}
            chunk = save_entity(db, "knowledge_chunk", content, status="CANDIDATE", parent_id=revision.id) if persist else Entity(
                id=new_id(), kind="knowledge_chunk", data=content, status="CANDIDATE", parent_id=revision.id)
            chunks.append(chunk)
    if persist:
        db.flush()
    return chunks


def index_chunks(db, chunks, *, checkpoint=None):
    if not chunks:
        raise ValueError("no_searchable_content")
    if MODE == "CORE":
        return {"mode": "LEXICAL_ONLY", "indexed": len(chunks), "warning": "CORE_LIMITED_MODE"}
    _request(QDRANT + f"/collections/{COLLECTION}", {"vectors": {"dense": {"size": 384, "distance": "Cosine"}},
             "sparse_vectors": {"lexical": {"modifier": "idf"}}}, method="PUT") if not _collection_exists() else None
    fingerprints = set()
    for offset in range(0, len(chunks), 8):
        if checkpoint:
            checkpoint()
        batch = chunks[offset:offset + 8]
        embedded = _request(EMBEDDER + "/embed", {"texts": [c.data["text"] for c in batch], "purpose": "document"}, timeout=90)
        fingerprints.add(embedded["artifact_fingerprint"])
        points = []
        for c, vector in zip(batch, embedded["vectors"]):
            if len(vector) != 384:
                raise ValueError("embedding_dimension_mismatch")
            points.append({"id": c.id, "vector": {"dense": vector, "lexical": sparse(c.data["text"])},
                           "payload": {"revision_id": c.parent_id, "document_id": c.data["document_id"],
                                       "block_id": c.data["block_id"], "org_unit_ids": c.data.get("org_unit_ids", []), "encoder_fingerprint": embedded["artifact_fingerprint"]}})
        _request(QDRANT + f"/collections/{COLLECTION}/points?wait=true", {"points": points}, method="PUT", timeout=90)
    if len(fingerprints) != 1:
        raise ValueError("mixed_embedding_artifacts")
    return {"mode": "DENSE_SPARSE", "indexed": len(chunks), "encoder_fingerprint": next(iter(fingerprints)), "collection": COLLECTION}


def _collection_exists():
    try:
        _request(QDRANT + f"/collections/{COLLECTION}", method="GET", timeout=10)
        return True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return False
        raise


def substantive_evidence(text):
    """A title/date is a locator, not sufficient support for an answer."""
    words = lexical_terms(text)
    if not any(not word.isdecimal() for word in words):
        return False
    return len(words) >= 18 or len(words) >= 2 and bool(re.search(r"[.!](?:\s|$)|[—–]\s*\S|\bэто\b", text))


def reviewed_figure_text(block):
    if (block.get("kind") != "FIGURE" or block.get("review_status") != "REVIEWED"
            or block.get("disposition") in {"REFERENCE_ONLY", "EXCLUDE_NONCONTENT"}
            or block.get("description_origin") != "HR_AUTHORED"
            or not block.get("image_sha256")
            or block.get("description_image_sha256") != block["image_sha256"]):
        return ""
    return block.get("description", "").strip()


def contextual_candidate(db, chunk):
    """Canonical page context restores detached slide headings and conditions.

    This is a read-only retrieval view. Published chunks and source extraction
    remain immutable, and every added span has its original block id/hash.
    """
    extraction = db.get(Entity, chunk.data.get("extraction_revision_id"))
    if not extraction or extraction.kind != "extraction":
        return chunk
    if extraction.parent_id != chunk.parent_id:
        return SimpleNamespace(id=chunk.id, parent_id=chunk.parent_id,
                               data={**chunk.data, "context_components": [], "evidence_eligible": False})
    page = [b for b in extraction.data.get("blocks", []) if b.get("page_no") == chunk.data["page_no"]]
    blocks = [b for b in page
              if b.get("kind") in {"TEXT", "TABLE"} and b.get("disposition") not in {"REFERENCE_ONLY", "EXCLUDE_NONCONTENT"} and b.get("text", "").strip()]
    primary = next((b for b in page if b["id"] == chunk.data["block_id"]), None)
    ranges = [{"block_id": b["id"], "char_start": 0, "char_end": len(b["text"]), "text": b["text"], "sha256": digest(b["text"].encode())} for b in blocks]
    native = "\n\n".join(b["text"] for b in blocks)
    components, figures = [], []
    if substantive_evidence(native) and blocks:
        representative = primary if primary and primary.get("kind") in {"TEXT", "TABLE"} else blocks[0]
        components.append({**chunk.data, "kind": "TEXT", "block_id": representative["id"],
                           "bbox": representative.get("bbox"), "parent_text": native,
                           "parent_ranges": ranges, "image_sha256": None,
                           "block_text_sha256": digest(representative["text"].encode())})
    for block in page:
        description = reviewed_figure_text(block)
        if not description or not substantive_evidence(description):
            continue
        figures.append(description)
        components.append({**chunk.data, "kind": "FIGURE", "block_id": block["id"],
                           "bbox": block.get("bbox"), "text": description,
                           "parent_text": description, "parent_ranges": None,
                           "image_sha256": block["image_sha256"],
                           "block_text_sha256": digest(description.encode())})
    ranking_context = "\n\n".join([native] + figures).strip()
    return SimpleNamespace(id=chunk.id, parent_id=chunk.parent_id, data={**chunk.data,
        "parent_text": ranking_context, "ranking_text": ranking_context,
        "section_heading": section_heading(native),
        "parent_ranges": ranges, "context_components": components,
        "evidence_eligible": bool(components),
        "block_text_sha256": digest((primary.get("text", "") if primary else chunk.data["text"]).encode())})


def query_terms_with_equivalents(query):
    # Explicit HR vocabulary is retrieval-only; it grants no scope and supplies
    # no answer facts. The employee's original question reaches the generator.
    equivalents = []
    extra = []
    if re.search(r"\bувольн[а-я]*", query.casefold()):
        extra.append("расторжении прекращении прекращения трудового договора")
        equivalents.append("EMPLOYMENT_TERMINATION")
    if re.search(r"(?:что\s+(?:относится|входит|включает|является|считается)|какие\s+(?:виды|сведения|категории|условия|требования)|(?:полный|весь)\s+перечень)", query.casefold()):
        if re.search(r"\b(?:услови[а-я]*|требовани[а-я]*|критери[а-я]*)\b", query.casefold()):
            extra.append("критерии условия требования")
        else:
            pass  # A list intent is metadata; adding generic nouns pollutes recall.
        equivalents.append("DEFINITION_OR_ENUMERATION")
    if re.search(r"\bсотрудник[а-я]*\b|\bперсонал[а-я]*\b", query.casefold()):
        extra.append("работник работники")
        equivalents.append("EMPLOYEE_VOCABULARY")
    if re.search(r"\bобязанност[а-я]*\b", query.casefold()):
        extra.append("обязан должен")
        equivalents.append("DUTY_VOCABULARY")
    if re.search(r"\bсоблюда[а-я]*\b", query.casefold()):
        extra.append("соблюдение соблюдения обязательство")
        equivalents.append("VERBAL_NOMINAL_COMPLIANCE")
    if audience_scope_question(query):
        extra.append("область применения распространяется деятельность лица участники")
        equivalents.append("DOCUMENT_AUDIENCE_SCOPE")
    if re.search(r"коммерческ[а-я]*\s+тайн[а-я]*", query.casefold()):
        extra.append("КТ")
        equivalents.append("CONFIDENTIALITY_ABBREVIATION")
    return query + (" " + " ".join(extra) if extra else ""), equivalents


def rerank_passages(chunk, cache):
    parent = chunk.data.get("ranking_text", chunk.data.get("parent_text", chunk.data["text"]))
    if parent == chunk.data["text"]:
        return [chunk.data["text"]]
    key = (chunk.data.get("extraction_revision_id"), chunk.data["page_no"])
    if key not in cache:
        pieces = _request(RERANKER + "/chunks", {"text": parent, "max_tokens": 220})["chunks"]
        head = _request(RERANKER + "/chunks", {"text": parent[:400], "max_tokens": 32})["chunks"][0]["text"] if len(pieces)>1 else ""
        cache[key] = pieces, head
    pieces, head = cache[key]
    if len(pieces)<=2:
        chosen=range(len(pieces))
    else:
        offset=parent.find(chunk.data["text"])
        offset=max(0,offset)
        focus=max(range(len(pieces)),key=lambda i:max(0,min(offset+len(chunk.data["text"]),pieces[i]["char_end"])-max(offset,pieces[i]["char_start"])))
        chosen=sorted({focus, min(focus+1,len(pieces)-1) if focus+1<len(pieces) else max(0,focus-1)})
    return [(head+"\n" if i and head else "") + pieces[i]["text"] for i in chosen]


def bm25(query, chunks):
    query_terms = set(lexical_terms(query))
    documents = [collections.Counter(lexical_terms(c.data["text"])) for c in chunks]
    frequencies = collections.Counter(term for document in documents for term in document)
    avg_len = sum(sum(c.values()) for c in documents) / max(len(documents), 1)
    ranked = []
    for chunk, counts in zip(chunks, documents):
        score = 0.0
        for term in query_terms:
            df = frequencies[term]
            if not df or term not in counts:
                continue
            tf = counts[term]
            idf = math.log(1 + (len(documents) - df + .5) / (df + .5))
            score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * sum(counts.values()) / max(avg_len, 1)))
        if score > 0:
            ranked.append((chunk, score))
    return sorted(ranked, key=lambda pair: pair[1], reverse=True)


def search(db, employee, query, limit=6, _preview=None):
    employee = current_employee(db, employee)
    if not employee_data(employee) or employee_data(employee).get("enabled", True) is False:
        return [], {"reason": "EMPLOYEE_DISABLED", "mode": MODE}
    original_query = query
    head = db.scalar(select(Entity).where(Entity.kind == "knowledge_head").execution_options(populate_existing=True)) if not _preview else None
    snapshot = db.get(Entity, head.data.get("snapshot_id")) if head else None
    permitted = _preview["permitted"] if _preview else published_corpus(db, employee)
    if not permitted:
        return [], {"reason": "NO_PUBLISHED_APPLICABLE_SOURCES", "mode": MODE}
    statuses = ["CANDIDATE", "PUBLISHED"] if _preview else ["PUBLISHED"]
    chunks = db.scalars(select(Entity).where(Entity.kind == "knowledge_chunk", Entity.parent_id.in_(list(permitted)), Entity.status.in_(statuses))).all()
    if _preview:
        changed = set(_preview["changed_revision_ids"])
        frozen_chunks = set(_preview["chunk_ids"])
        chunks = [c for c in chunks if c.parent_id not in changed or c.id in frozen_chunks]
    by_id = {c.id: c for c in chunks}
    query, equivalent_ids = query_terms_with_equivalents(query)
    timings = {key: None for key in ("embedding_seconds", "vector_query_seconds", "rerank_preparation_seconds", "rerank_seconds", "lexical_seconds")}
    with _timed_stage(timings, "lexical_seconds"):
        lexical = bm25(query, chunks)
        structural = {chunk.id: audience_scope_signal(original_query, chunk.data.get("parent_text", chunk.data["text"]), permitted[chunk.parent_id][0].data["title"])
                      + definition_signal(original_query, chunk.data.get("parent_text", chunk.data["text"])) for chunk in chunks}
        structural = {cid: value for cid, value in structural.items() if value}
    snapshot_consistent = bool(snapshot and snapshot.kind == "knowledge_snapshot" and all(snapshot.data.get("members", {}).get(doc.id) == revision.id for doc, revision in permitted.values()))
    chunker_identities = sorted({(c.data.get("chunker_version", "LEGACY_UNVERSIONED"), c.data.get("chunker_fingerprint", "LEGACY_UNVERSIONED")) for c in chunks})
    metadata = {"mode": MODE, "retrieval": "BM25", "kb_revision_ids": sorted(permitted), "query_equivalents": equivalent_ids, "timings": timings,
                "pipeline_identities": pipeline_identities(), "observed_chunker_identities": [{"version": version, "fingerprint": fp} for version, fp in chunker_identities],
                "structural_recall_candidates": len(structural),
                "published_snapshot_id": snapshot.id if snapshot_consistent else None,
                "snapshot_identity_status": "EXACT_PUBLISHED_MEMBERSHIP" if snapshot_consistent else "EDITOR_CANDIDATE" if _preview else "UNAVAILABLE_OR_CONCURRENT_HEAD_CHANGE"}
    if MODE == "CORE":
        candidates, pages = [], set()
        lexical_ids = {chunk.id for chunk, _ in lexical}
        core_candidates = lexical + [(by_id[cid], 0.0) for cid in structural if cid not in lexical_ids]
        for rank, (chunk, score) in enumerate(core_candidates, 1):
            contextual = contextual_candidate(db, chunk)
            key = (chunk.parent_id, chunk.data["page_no"])
            if key in pages or contextual.data.get("evidence_eligible") is False:
                continue
            if "context_components" not in contextual.data and not substantive_evidence(contextual.data.get("parent_text", contextual.data["text"])):
                continue
            pages.add(key)
            document_title = permitted[chunk.parent_id][0].data["title"]
            parent_text = contextual.data.get("parent_text", contextual.data["text"])
            topic = heading_topic_signal(original_query, parent_text, document_title)
            coverage = topic_alignment(original_query, parent_text, document_title)
            candidates.append((contextual, 1 / (60 + rank) + .02 * topic + .03 * coverage + .06 * structural.get(chunk.id, 0), permitted[chunk.parent_id]))
        return sorted(candidates, key=lambda item: item[1], reverse=True)[:limit], metadata
    try:
        selected = selected_model_fingerprints(db)
        with _timed_stage(timings, "embedding_seconds"):
            embedded = _request(EMBEDDER + "/embed", {"texts": [query], "purpose": "query", **({"artifact_fingerprint": selected["embedder"]} if selected.get("embedder") else {})}, timeout=15)
        if selected and embedded["artifact_fingerprint"] != selected.get("embedder"):
            return [], {**metadata, "reason": "DEGRADED_PROFILE_MISMATCH", "degraded": True}
        indexed_fingerprints = {revision.data.get("index_projection", {}).get("encoder_fingerprint") for doc, revision in permitted.values()
                                if revision.data.get("index_projection", {}).get("mode") != "REFERENCE_ONLY"}
        if indexed_fingerprints != {embedded["artifact_fingerprint"]}:
            return [], {**metadata, "reason": "INDEX_MODEL_FINGERPRINT_MISMATCH", "degraded": True}
        filter_ = {"must": [{"key": "revision_id", "match": {"any": list(permitted)}}]}
        with _timed_stage(timings, "vector_query_seconds"):
            dense = _request(QDRANT + f"/collections/{COLLECTION}/points/query", {
                "query": embedded["vectors"][0], "using": "dense", "filter": filter_, "limit": 30, "with_payload": True})["result"]["points"]
            sparse_result = _request(QDRANT + f"/collections/{COLLECTION}/points/query", {
                "query": sparse(query), "using": "lexical", "filter": filter_, "limit": 30, "with_payload": True})["result"]["points"] if terms(query) else []
        scores = collections.defaultdict(float)
        for branch in [dense, sparse_result]:
            for rank, point in enumerate(branch, 1):
                if str(point["id"]) in by_id:
                    scores[str(point["id"])] += 1 / (60 + rank)
        # Russian inflections and numbered appendices need a lexical recall
        # branch independent of the existing wire sparse vocabulary. These DB
        # chunks have already passed the exact current corpus/ACL restriction.
        for rank, (chunk, _) in enumerate(lexical[:30], 1):
            scores[chunk.id] += 1 / (60 + rank)
        for cid, signal in structural.items():
            scores[cid] += .08 * signal
        selected, parent_keys = [], set()
        def parent_key(chunk):
            return (chunk.parent_id, chunk.data.get("page_no"))
        for cid in sorted(scores, key=scores.get, reverse=True):
            key = parent_key(by_id[cid])
            if key in parent_keys:
                continue
            parent_keys.add(key)
            selected.append(cid)
            if len(selected) == 12:
                break
        if not selected:
            return [], {**metadata, "reason": "NO_MATCHES"}
        selected_profile = selected_model_fingerprints(db)
        passage_cache, pairs = {}, []
        with _timed_stage(timings, "rerank_preparation_seconds"):
            for cid in selected:
                by_id[cid] = contextual_candidate(db, by_id[cid])
            selected = [cid for cid in selected if by_id[cid].data.get("evidence_eligible") is not False
                        and (by_id[cid].data.get("context_components") or substantive_evidence(by_id[cid].data.get("parent_text", by_id[cid].data["text"])))]
            if not selected:
                return [], {**metadata, "reason": "INSUFFICIENT_SUBSTANTIVE_EVIDENCE"}
            for cid in selected:
                pairs.extend((cid, text) for text in rerank_passages(by_id[cid], passage_cache))
        with _timed_stage(timings, "rerank_seconds"):
            reranked = _request(RERANKER + "/rerank", {"query": query, "documents": [text for _,text in pairs], **({"artifact_fingerprint": selected_profile["reranker"]} if selected_profile.get("reranker") else {})}, timeout=25)
        if selected_profile and reranked["artifact_fingerprint"] != selected_profile.get("reranker"):
            return [], {**metadata, "reason": "DEGRADED_PROFILE_MISMATCH", "degraded": True}
        contextual_scores = {}
        for (cid, _), score in zip(pairs, reranked["scores"]):
            contextual_scores[cid] = max(score,contextual_scores.get(cid,float('-inf')))
        rerank_order = sorted(contextual_scores, key=contextual_scores.get, reverse=True)
        parent_lexical_ranks = {}
        for rank, (chunk, _) in enumerate(lexical, 1):
            parent_lexical_ranks.setdefault(parent_key(chunk), rank)
        lexical_ranks = {cid: parent_lexical_ranks[parent_key(by_id[cid])] for cid in selected if parent_key(by_id[cid]) in parent_lexical_ranks}
        candidate_ranks = {cid: rank for rank,cid in enumerate(selected,1)}
        # A generic cross-encoder may prefer a company-name header over a rare,
        # exact policy heading. Preserve independent lexical evidence through
        # final rank fusion; a reranker is a signal, not an authority override.
        use_heading = True
        fusion = {cid:1/(60+rank)+1/(60+candidate_ranks[cid])+(2/(60+lexical_ranks[cid]) if cid in lexical_ranks else 0)
                  + (.02 * heading_topic_signal(original_query, by_id[cid].data.get("parent_text",by_id[cid].data["text"]), permitted[by_id[cid].parent_id][0].data["title"]) if use_heading else 0)
                  + .03 * topic_alignment(original_query, by_id[cid].data.get("parent_text",by_id[cid].data["text"]), permitted[by_id[cid].parent_id][0].data["title"])
                  + .06 * structural.get(cid, 0)
                  for rank,cid in enumerate(rerank_order,1)}
        candidates = [(cid,contextual_scores[cid]) for cid in sorted(fusion,key=fusion.get,reverse=True)]
        metadata.update(retrieval="DENSE_BM25_RRF_RERANK", encoder_fingerprint=embedded["artifact_fingerprint"],
                        reranker_fingerprint=reranked["artifact_fingerprint"], reranking_pairs=len(pairs), structural_context="CANONICAL_PAGE_PARENT",
                        final_rank_fusion="INITIAL_RRF_RERANK_LEXICAL_RRF")
        metadata.update(lexical_normalization="SNOWBALL_RUSSIAN_3_1_1", candidate_diversification="ONE_CANONICAL_PARENT_PER_PAGE", recall_branches=3)
        metadata.update(heading_topic_signal=use_heading, lexical_final_rank_weight=2)
        # Score is a ranking signal, not a probability of correctness. Gate blocks
        # strongly negative pairs; human evaluation can tighten this setting.
        threshold = float(os.environ.get("RERANK_MIN_LOGIT", "-4"))
        return [(by_id[c], s, permitted[by_id[c].parent_id]) for c, s in candidates[:limit] if s >= threshold or structural.get(c)], metadata
    except httpx.HTTPStatusError as exc:
        reason = "QUERY_TOO_LONG" if exc.response.status_code == 422 else "DEGRADED_PROFILE_MISMATCH" if exc.response.status_code == 409 else "LOCAL_RETRIEVAL_UNAVAILABLE"
    except Exception:
        reason = "LOCAL_RETRIEVAL_UNAVAILABLE"
    # FULL never silently turns a failing reranker into unvalidated hybrid answers.
    return [], {**metadata, "reason": reason, "degraded": True}


def critical_normative_text(text):
    return bool(re.search(r"\b(?:запрещ[а-я]*|не\s+допуска[а-я]*|не\s+разреш[а-я]*|не\s+име[а-я]*\s+прав[а-я]*|не\s+вправе|не\s+разглашать|только\s+(?:после|при|по)|при\s+условии|долж(?:ен|на|но|ны)|обязан(?:а|о|ы)?|обязуется|обязуются|необходимо|требуется|в\s+обязательном\s+порядке)\b", text.casefold()))


def validate_generated(text, evidence):
    """Conservative mechanical checks, not a claim of semantic verification."""
    if not text or len(text) > 3500 or "<think>" in text or "</think>" in text:
        return False, "INVALID_GENERATED_OUTPUT"
    citations = [int(v) for v in re.findall(r"\[(\d+)\]", text)]
    if not citations or any(v < 1 or v > len(evidence) for v in citations):
        return False, "INVALID_OR_MISSING_CITATION"
    source = "\n".join(e["text"] for e in evidence)
    # Dates, quantities, phone numbers and URLs must occur verbatim in evidence.
    number_pattern = r"\b\d+(?:[.,:/-]\d+)*\b"
    contact_pattern = r"https?://\S+|[\w.+-]+@[\w.-]+\.\w+"
    for number in re.findall(number_pattern, re.sub(r"\[\d+\]", "", text)):
        if number not in set(re.findall(number_pattern, source)):
            return False, "UNSUPPORTED_NUMBER"
    for url in re.findall(contact_pattern, text):
        if url not in set(re.findall(contact_pattern, source)):
            return False, "UNSUPPORTED_CONTACT"
    # Each claim must be supported by the source it actually cites, rather than
    # another retrieved page or a previous assistant answer. This conservative
    # lexical rejection is not an entailment model or a semantic truth claim.
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
        refs = [int(value) for value in re.findall(r"\[(\d+)\]", sentence)]
        claim = set(lexical_terms(re.sub(r"\[\d+\]", "", sentence)))
        if not claim:
            continue
        if not refs:
            # A citation on its own following line may apply to this sentence.
            following = text[text.find(sentence) + len(sentence):].lstrip()
            refs = [int(value) for value in re.findall(r"\[(\d+)\]", following.split("\n", 1)[0])]
        if not refs or any(value < 1 or value > len(evidence) for value in refs):
            return False, "UNCITED_GENERATED_CLAIM"
        cited = "\n".join(evidence[value - 1]["text"] for value in refs)
        claim_text = re.sub(r"\[\d+\]", "", sentence)
        if any(number not in set(re.findall(number_pattern, cited)) for number in re.findall(number_pattern, claim_text)):
            return False, "UNSUPPORTED_CITED_NUMBER"
        if any(contact not in set(re.findall(contact_pattern, cited)) for contact in re.findall(contact_pattern, claim_text)):
            return False, "UNSUPPORTED_CITED_CONTACT"
        if critical_normative_text(cited):
            return False, "NORMATIVE_REWRITE_REQUIRES_LITERAL"
        supported = set(lexical_terms(cited))
        if not substantive_evidence(cited) or len(claim & supported) / len(claim) < .55:
            return False, "UNSUPPORTED_CITED_CLAIM"
    return True, "MECHANICAL_CHECKS_PASSED"


def safe_history(db, employee, context, conversation_id=None):
    """Only caller-owned employee/bot messages; current source ACL wins history."""
    employee = current_employee(db, employee)
    if not employee_data(employee) or employee_data(employee).get("enabled", True) is False:
        return []
    permitted = published_corpus(db, employee)
    rows = context if isinstance(context, list) else (context or {}).get("messages", []) if isinstance(context, dict) else []
    accepted = []
    for row in rows[-12:]:
        if hasattr(row, "author_type"):
            if conversation_id and row.conversation_id != conversation_id:
                continue
            role, text, data = row.author_type, row.text, row.data or {}
        elif isinstance(row, dict):
            role, text, data = row.get("author_type", row.get("role", "")), row.get("text", row.get("content", "")), row.get("data", {}) or {}
            if conversation_id:
                cid = row.get("conversation_id")
                if not cid and row.get("id"):
                    persisted = db.get(Message, row["id"])
                    cid = persisted.conversation_id if persisted else None
                if cid != conversation_id:
                    continue
        else:
            continue
        role = role.upper()
        if role not in {"USER", "EMPLOYEE", "BOT", "ASSISTANT"}:
            continue
        citations = data.get("citations", [])
        if any(c.get("revision_id") not in permitted for c in citations if isinstance(c, dict)):
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        if role in {"USER", "EMPLOYEE"} and unsupported_secret_or_tool_request(text):
            continue
        accepted.append({"role": "user" if role in {"USER", "EMPLOYEE"} else "assistant", "content": text[:1200]})
    # Exact 800-token history cap is applied by the generator's own tokenizer.
    # Character cap here also bounds transport/parser work before tokenization.
    accepted = accepted[-6:]
    while sum(len(row["content"]) for row in accepted) > 3000:
        accepted.pop(0)
    return accepted


def _followup_query(question, history):
    vague = bool(re.fullmatch(r"\s*(?:а\s+)?(?:когда|где|как|почему|зачем|сколько|куда)(?:\s+(?:это|они|он|она|там|тогда))?[?!. ]*", question.lower()))
    dependent = vague or bool(re.search(r"\b(?:это|этого|этому|этим|этом|там|тогда|такого|такие|их)\b", question.lower()))
    if not dependent:
        return question, False
    prior = next((row["content"] for row in reversed(history) if row["role"] == "user" and row["content"].strip() != question.strip()), None)
    if not prior:
        return question, True
    return prior[:250] + "\nУточнение: " + question, False


def unsupported_secret_or_tool_request(question):
    """Detect direct exfiltration/tool imperatives, not discussion of policies.

    An ordinary question about how to transmit passwords remains a retrieval
    question. No model, history or document can authorize server/secret access.
    """
    text = question.casefold().replace("ё", "е")
    start = r"(?:^|[.!?\n]\s*)\s*(?:(?:пожалуйста|теперь|сначала)[, ]+)?"
    if re.search(start + r"(?:игнорируй(?:те)?|забудь(?:те)?|обойди(?:те)?|ignore|disregard)\b.{0,140}(?:правил|инструкц|system|документ|промпт|previous)", text):
        return True
    for match in re.finditer(start + r"(?:покажи(?:те)?|выведи(?:те)?|пришли(?:те)?|напиши(?:те)?|раскрой(?:те)?|распечатай(?:те)?|прочитай(?:те)?|дай(?:те)?|print|reveal|dump)\b(.{0,180})", text):
        target = match.group(1)
        if re.search(r"\b(?:регламент|порядок|правила|инструкц|политик)", target):
            continue
        if re.search(r"(?:секретн.{0,30}(?:токен|ключ|парол)|api[ _-]*(?:token|key|токен|ключ)|токен.{0,20}бот|парол.{0,20}сервер|переменн.{0,25}окружени|системн.{0,20}промпт|system\s*prompt|private\s*key|os\.environ|/proc/|/etc/)", target):
            return True
    return bool(re.search(start + r"(?:выполни(?:те)?|запусти(?:те)?|execute|run)\b.{0,80}(?:curl|wget|команд|shell|bash|python|cat\s+/)", text))


def personal_hr_fact_question(question):
    text = question.casefold().replace("ё", "е")
    patterns = [
        r"\b(?:мой|моя|мое|мои|у\s+меня)\b.{0,30}(?:зарплат|оклад|преми|испытательн|увольнен)",
        r"\bсколько\b.{0,15}\b(?:я|мне)\b.{0,25}(?:получ|зарабат|заплат|выплат|начисл)",
        r"\b(?:мне|меня)\b.{0,25}(?:начисл|выплат|заплат|одобр|назнач|увол|повысил|отказал)",
        r"\bя\b.{0,20}(?:прошел|прошла|прошли|прохожу|закончил|закончила)\b.{0,25}(?:испыт|срок)",
        r"\b(?:прошел|прошла|пройден)\b.{0,15}\b(?:я|мной|мною)\b.{0,20}испыт",
    ]
    return any(re.search(pattern,text) for pattern in patterns)


def literal_normative_parent(question,evidence):
    """Permissions and prohibitions retain the complete normative source.

    Small generative models can invert a negation while preserving citations.
    This policy therefore forbids rewriting critical yes/no and duty clauses.
    """
    def topic_recall(text):
        fillers = {"когда", "нужн", "необходим", "следова", "каких", "услов", "компан", "обществ"}
        query = {word for word in lexical_terms(query_terms_with_equivalents(question)[0]) if len(word) >= 4 or word in {"кт", "ки"}} - fillers
        source = set(lexical_terms(text))
        return sum(any(word == candidate or (min(len(word), len(candidate)) >= 5 and (word.startswith(candidate) or candidate.startswith(word)) and min(len(word), len(candidate))/max(len(word), len(candidate)) >= .8) for candidate in source) for word in query)
    primary_recall = topic_recall(evidence[0]["text"]) if evidence else 0
    for index,parent in enumerate(evidence):
        # A generic secondary prohibition cannot replace a more applicable
        # primary answer about another action (e.g. signing a contract).
        recall = topic_recall(parent["text"])
        if recall < primary_recall or index > 0 and recall == primary_recall:
            continue
        # Exact modal forms avoid treating «на должном уровне» or the noun
        # «обязательство» as an instruction about an unrelated action.
        if critical_normative_text(parent['text']):
            return index
    return None


def source_completeness_guard(question, evidence):
    """A reference to an unavailable full list is not the full list itself."""
    requested = bool(re.search(r"(?:полн[а-я]*\s+переч[а-я]*|весь\s+перечень|(?:приведи|покажи|пришли|дай|скачать).{0,100}приложен)", question.casefold()))
    if not requested:
        return None
    if any(re.search(r"(?:^|\n)\s*приложение\s*(?:№|n)?\s*\d+\s*(?:\n|$)", e["text"].casefold()) for e in evidence):
        return None
    for index, parent in enumerate(evidence):
        text = re.sub(r"\s+", " ", parent["text"].casefold())
        if re.search(r"полн[а-я]*\s+переч[а-я]*.{0,500}(?:ознаком[а-я]*|изложен[а-я]*|размещен[а-я]*|положен[а-я]*).{0,300}приложен", text):
            return index
    return None


def enumeration_parent(question, evidence):
    if ("DEFINITION_OR_ENUMERATION" not in query_terms_with_equivalents(question)[1]
            and not re.search(r"\b(?:какие|каковы|перечисли|список|перечень|все)\b", question.casefold())):
        return None
    for index, parent in enumerate(evidence):
        text = parent["text"]
        bullets = len(re.findall(r"(?:^|\n)\s*[-•–]\s+", text))
        lines = [line for line in text.splitlines() if len(lexical_terms(line)) >= 3]
        if bullets >= 3 or text.count(";") >= 2 or len(lines) >= 4:
            return index
    return None


def complementary_parent_indices(question, evidence, citations, headings, selected):
    """Keep related complete parents; a selected duty is not the whole section."""
    if audience_scope_question(question):
        scope = [i for i, item in enumerate(evidence) if audience_scope_signal(question, item["text"], item["title"])]
        revisions = {citations[i]["revision_id"] for i in scope}
        return [i for i, item in enumerate(evidence) if i in scope or citations[i]["revision_id"] in revisions and critical_normative_text(item["text"])]
    definitions = [i for i, item in enumerate(evidence) if definition_signal(question, item["text"])]
    if definitions:
        revisions = {citations[i]["revision_id"] for i in definitions}
        focus = set(lexical_terms(question)) - {"что", "так", "означа", "счита", "явля", "относ", "как", "определя", "понима", "определен", "компан", "обществ", "пожалуйст", "подскаж", "хоч", "уточн", "объясн", "правил"}
        return [i for i in range(len(evidence)) if i in definitions or citations[i]["revision_id"] in revisions and bool(focus) and len(focus & set(headings[i].split())) / len(focus) >= .5]
    if selected is None:
        return []
    return [i for i in range(len(evidence)) if i == selected or headings[selected] and headings[i] == headings[selected] and citations[i]["revision_id"] == citations[selected]["revision_id"]]


def source_scoped_procedure(question, evidence):
    """A procedure under an extra explicit antecedent is not a general one."""
    if not re.search(r"(?:как\s+(?:поступить|поступать|действовать)|что\s+делать|порядок\s+действий)", question.casefold()):
        return False
    query = set(lexical_terms(question))
    for item in evidence:
        for match in re.finditer(r"\bв\s+случае\s+([^,\n]{1,350})", item["text"].casefold()):
            conditional = match.group(1)
            extra = re.search(r"\b(?:при|во\s+время|в\s+связи\s+с)\s+(.+)$", conditional)
            if not extra:
                continue
            focus = set(lexical_terms(conditional[:extra.start()])) - {"возникновен", "возможност", "случа", "его", "или"}
            qualifier = set(lexical_terms(extra.group(1)))
            if focus & query and len(qualifier - query) >= 2:
                return True
    return False


def citation_footer(citations):
    """Bound display labels only; canonical titles and evidence stay in citations."""
    labels = []
    for index, citation in enumerate(citations, 1):
        title = str(citation["document_title"])
        title = title if len(title) <= 120 else title[:119] + "…"
        labels.append(f"[{index}] {title}, стр. {citation['page_no']}")
    footer = "\n\nИсточники: " + "; ".join(labels)
    if len(footer) > 1000:
        footer = "\n\nИсточники: " + "; ".join(f"[{index}] документ, стр. {str(citation['page_no'])[:24]}" for index, citation in enumerate(citations, 1))
    return footer


def expand_section_context(db, employee, question, ranked, _preview=None):
    """Recall complementary published parents outside the vector/reranker top K.

    A repeated physical heading identifies a section inside one immutable
    extraction. Query subject words restrict added companions; existing selected
    section parents remain intact. This supplies source context, not embeddings
    or a fabricated cross-encoder score.
    """
    if not ranked:
        return ranked, {}
    first, _, (doc, revision) = ranked[0]
    first = first if "context_components" in first.data else contextual_candidate(db, first)
    heading = first.data.get("section_heading")
    extraction_id = first.data.get("extraction_revision_id")
    if not heading or not critical_normative_text(first.data.get("parent_text", first.data["text"])):
        return ranked, {}
    permitted = _preview["permitted"] if _preview else published_corpus(db, employee)
    extraction = db.get(Entity, extraction_id)
    if revision.id not in permitted or not extraction or extraction.kind != "extraction" or extraction.parent_id != revision.id:
        return ranked, {}
    # The infinitive is the requested action; nouns/adjectives identify its
    # subject. Repeated document/section title words alone add no new topic.
    fillers = {"как", "каки", "каков", "что", "кто", "нужн", "необходим", "можн", "услов", "требован", "перед", "посл", "при", "компан", "обществ", "пожалуйст", "подскаж", "хоч", "уточн", "объясн", "правил"}
    subject_words = [word for word in re.findall(r"[а-яёa-zA-Z]+", question.casefold()) if not word.endswith(("ть", "ться", "ти", "чь"))]
    focus = set(lexical_terms(" ".join(subject_words))) - fillers - set(heading.split()) - set(lexical_terms(doc.data["title"]))
    section, seen_pages = [], set()
    original_pages = set()
    for chunk, rank_score, pair in ranked[:3]:
        if chunk.parent_id != revision.id or chunk.data.get("extraction_revision_id") != extraction_id:
            continue
        candidate = chunk if "context_components" in chunk.data else contextual_candidate(db, chunk)
        if candidate.data.get("section_heading") == heading and candidate.data.get("evidence_eligible") is not False:
            page = candidate.data["page_no"]
            if page not in seen_pages:
                section.append((candidate, rank_score, pair)); seen_pages.add(page); original_pages.add(page)
    statuses = ["CANDIDATE", "PUBLISHED"] if _preview else ["PUBLISHED"]
    candidates = db.scalars(select(Entity).where(Entity.kind == "knowledge_chunk", Entity.parent_id == revision.id, Entity.status.in_(statuses))).all()
    frozen = set(_preview.get("chunk_ids", [])) if _preview and revision.id in set(_preview.get("changed_revision_ids", [])) else None
    for chunk in candidates:
        if chunk.data.get("extraction_revision_id") != extraction_id or chunk.data["page_no"] in seen_pages or frozen is not None and chunk.id not in frozen:
            continue
        candidate = contextual_candidate(db, chunk)
        parent = candidate.data.get("parent_text", candidate.data["text"])
        # An e-mail destination mentions the adjective "electronic" without
        # describing the electronic document/file that a question may concern.
        # This affects recall only; canonical quoted text is never rewritten.
        subject_text = re.sub(r"\b(?:электронн[а-я]*\s+почт[а-я]*|e[ -]?mail)\b", "", parent.casefold())
        if candidate.data.get("section_heading") != heading or candidate.data.get("evidence_eligible") is False or focus and not (focus & set(lexical_terms(subject_text))):
            continue
        section.append((candidate, 0.0, (doc, revision))); seen_pages.add(candidate.data["page_no"])
        if len(seen_pages) > 6:
            return [], {"reason": "SECTION_CONTEXT_TOO_COMPLEX", "section_context_policy": "EXACT_PERMITTED_REVISION_EXTRACTION_HEADING"}
    if len(section) <= 1:
        return ranked, {}
    other = [item for item in ranked if not (item[0].parent_id == revision.id and item[0].data.get("extraction_revision_id") == extraction_id and item[0].data["page_no"] in seen_pages)]
    return section + other, {"section_context_policy": "EXACT_PERMITTED_REVISION_EXTRACTION_HEADING", "section_context_pages": sorted(seen_pages),
                             "section_context_added_pages": sorted(seen_pages - original_pages), "section_context_page_limit": max(3, len(section)),
                             "section_context_scores": "ADDED_PARENTS_HAVE_NO_MODEL_SCORE"}


def answer_question(db, conversation, question, context=None, _preview=None):
    pipeline_started = time.monotonic()
    employee_id = conversation.data.get("employee_id")
    employee = current_employee(db, {"employee_id": employee_id}) if employee_id else None
    if not employee or employee.kind != "employee":
        return {"mode": "HANDOFF", "text": "Для ответа нужно связать чат с вашей карточкой сотрудника. Обратитесь к HR.", "citations": []}
    if employee.data.get("enabled", True) is False:
        return {"mode": "HANDOFF", "text": "Доступ к помощнику приостановлен.", "citations": [], "evidence": [], "reason": "EMPLOYEE_DISABLED"}
    if unsupported_secret_or_tool_request(question):
        return {"mode": "HANDOFF", "text": "Я помогаю с адаптацией по опубликованным документам. По вопросам доступа к системам обратитесь к HR или ИТ-службе.",
                "citations": [], "evidence": [], "reason": "UNSUPPORTED_SECRET_OR_TOOL_REQUEST"}
    if personal_hr_fact_question(question):
        return {"mode": "HANDOFF", "text": "Это персональный кадровый вопрос. Его нужно уточнить у вашего HR; общие документы не подтверждают индивидуальное решение.",
                "citations": [], "evidence": [], "reason": "PERSONAL_HR_FACT"}
    history = safe_history(db, employee, context, conversation.id)
    retrieval_query, unresolved = _followup_query(question, history)
    if unresolved:
        return {"mode": "CLARIFY", "text": "Уточните, пожалуйста, о каком правиле или вопросе идёт речь?", "citations": [], "evidence": [], "reason": "UNRESOLVED_FOLLOWUP"}
    if personal_hr_fact_question(retrieval_query):
        return {"mode": "HANDOFF", "text": "Это персональный кадровый вопрос. Его нужно уточнить у вашего HR; общие документы не подтверждают индивидуальное решение.",
                "citations": [], "evidence": [], "reason": "PERSONAL_HR_FACT"}
    retrieval_started = time.monotonic()
    ranked, metadata = search(db, employee, retrieval_query, _preview=_preview)
    ranked, section_metadata = expand_section_context(db, employee, retrieval_query, ranked, _preview)
    metadata.update(section_metadata)
    metadata["timings"] = {**metadata.get("timings", {}), "retrieval_seconds": round(time.monotonic() - retrieval_started, 6),
        "generation_request_seconds": 0.0, "generation_queue_seconds": None, "generation_inference_seconds": None}
    metadata["history_messages"] = len(history)
    metadata["generation_history_policy"] = "QUESTION_RESOLUTION_ONLY_NO_PREVIOUS_ANSWERS"
    metadata["query_resolution"] = "BOUNDED_PREVIOUS_USER_QUESTION" if retrieval_query != question else "CURRENT_QUESTION"
    if not ranked:
        metadata["timings"]["pipeline_seconds"] = round(time.monotonic() - pipeline_started, 6)
        reason = metadata.get("reason", "INSUFFICIENT_EVIDENCE")
        text = "В опубликованных документах для вашей площадки не нашлось достаточно сведений. Могу передать вопрос HR."
        if reason == "QUERY_TOO_LONG":
            text = "Уточните, пожалуйста, один короткий вопрос — так я смогу проверить его по документам."
        elif metadata.get("degraded"):
            text = "Сейчас локальный поиск временно недоступен. Могу передать вопрос HR."
        if reason == "SECTION_CONTEXT_TOO_COMPLEX":
            text = "Найденный раздел содержит слишком много связанных условий для безопасного полного ответа. Уточните вопрос или обратитесь к HR."
        return {"mode": "CLARIFY" if reason == "QUERY_TOO_LONG" else "HANDOFF", "text": text,
                "citations": [], "evidence": [], "provenance": metadata, "reason": reason}
    evidence, citations, seen, pages = [], [], set(), set()
    evidence_revisions, evidence_headings = [], []
    for chunk, score, (doc, revision) in ranked:
        if "context_components" not in chunk.data:
            chunk = contextual_candidate(db, chunk)
        components = chunk.data.get("context_components", [chunk.data])
        if chunk.data.get("evidence_eligible") is False:
            continue
        page_key = (revision.id, chunk.data["page_no"])
        if page_key in pages:
            continue
        added = False
        for source in components:
            full_text = source.get("parent_text", source["text"])
            if not substantive_evidence(full_text):
                continue
            key = source["block_id"]
            dedup_key = tuple(span["block_id"] for span in source["parent_ranges"]) if source.get("parent_ranges") else key
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            added = True
            evidence.append({"text": full_text, "title": doc.data["title"], "score": round(score, 5)})
            evidence_revisions.append(revision)
            evidence_headings.append(chunk.data.get("section_heading", section_heading(full_text)))
            citations.append({"document_id": doc.id, "revision_id": revision.id, "document_title": doc.data["title"],
                              "page_no": source["page_no"], "block_id": key,
                              "extraction_revision_id": source["extraction_revision_id"],
                              "chunker_version": source.get("chunker_version", "LEGACY_UNVERSIONED"), "chunker_fingerprint": source.get("chunker_fingerprint", "LEGACY_UNVERSIONED"),
                              "bbox": source.get("bbox"), "source_sha256": revision.data.get("sha256"),
                              "kind": "TEXT" if source["kind"] != "FIGURE" else "VISUAL",
                              "evidence_text": full_text, "text_sha256": source.get("block_text_sha256", digest(full_text.encode())), "evidence_text_sha256": digest(full_text.encode()),
                              "evidence_ranges": (source.get("parent_ranges") or [{"block_id": key, "char_start": 0, "char_end": len(full_text), "text": full_text,
                                                   "sha256": digest(full_text.encode())}]) if source["kind"] != "FIGURE" else [],
                              "text_origin": "HR_AUTHORED" if source["kind"] == "FIGURE" else "SOURCE_TEXT",
                              "image_sha256": source.get("image_sha256"),
                              "source_url": f"/api/v1/knowledge/revisions/{revision.id}/source"})
        if added:
            pages.add(page_key)
        if len(pages) >= metadata.get("section_context_page_limit", 3):
            break
    if not evidence or len(evidence) > 6:
        return {"mode": "HANDOFF", "text": "В найденных источниках недостаточно полного проверенного контекста. Уточните вопрос или обратитесь к HR.",
                "citations": citations, "evidence": evidence, "reason": "INSUFFICIENT_SUBSTANTIVE_EVIDENCE" if not evidence else "PAGE_CONTEXT_TOO_COMPLEX", "provenance": metadata}
    if not evidence_topic_supported(retrieval_query, evidence):
        metadata["timings"]["pipeline_seconds"] = round(time.monotonic() - pipeline_started, 6)
        metadata["relevance_gate"] = "NO_CONTENT_FOCUS_OVERLAP"
        return {"mode": "HANDOFF", "text": "В опубликованных документах для вашей площадки не нашлось сведений по этому вопросу. Могу передать вопрос HR.",
                "citations": [], "evidence": [], "reason": "INSUFFICIENT_TOPIC_EVIDENCE", "provenance": metadata}
    top_revision = evidence_revisions[0]
    instruction = top_revision.data.get("instruction") or {}
    normalized_question = " ".join(terms(question))
    approved_card = top_revision.data.get("article_kind") == "APPROVED_INSTRUCTION" and normalized_question in {
        " ".join(terms(q)) for q in [instruction.get("question", "")] + instruction.get("question_variants", [])}
    mode = "APPROVED_CARD" if approved_card else "EXTRACT"
    generation_attempted = False
    answer_text = "По действующим документам:\n\n" + "\n\n".join(f"[{i}] {e['text']}" for i, e in enumerate(evidence, 1))
    if approved_card:
        answer_text = instruction["answer_text"]
    reason = "APPROVED_INSTRUCTION_MATCH" if approved_card else "CORE_LIMITED_MODE" if MODE == "CORE" else "GENERATOR_NOT_USED"
    audience_intent = audience_scope_question(retrieval_query) and not approved_card
    literal_index = literal_normative_parent(retrieval_query, evidence) if not approved_card and not audience_intent else None
    missing_index = source_completeness_guard(retrieval_query, evidence) if not approved_card and not audience_intent else None
    enumeration_index = enumeration_parent(retrieval_query, evidence) if not approved_card and literal_index is None and missing_index is None and not audience_intent else None
    related = complementary_parent_indices(retrieval_query, evidence, citations, evidence_headings, literal_index if literal_index is not None else enumeration_index) if not approved_card else []
    if audience_intent and not related:
        return {"mode": "HANDOFF", "text": "В найденных документах не удалось подтвердить полный круг лиц, на которых распространяются требования. Уточните документ или обратитесь к HR.",
                "citations": citations, "evidence": evidence, "reason": "AUDIENCE_SCOPE_NOT_CONFIRMED", "provenance": metadata}
    if missing_index is not None:
        evidence, citations = [evidence[missing_index]], [citations[missing_index]]
        mode, reason = "HANDOFF", "REFERENCED_FULL_SOURCE_REQUIRED"
        answer_text = ("Полного текста указанного перечня среди найденных источников нет. "
                       "В доступном документе указано:\n\n[1] " + evidence[0]["text"]
                       + "\n\nПолный документ или приложение нужно запросить у HR.")
        metadata["generation_guard"] = "REFERENCED_SOURCE_COMPLETENESS"
    elif related:
        evidence, citations = [evidence[i] for i in related], [citations[i] for i in related]
        answer_text = "Формулировки действующих документов:\n\n" + "\n\n".join(f"[{i}] {item['text']}" for i, item in enumerate(evidence, 1))
        if audience_intent:
            reason, guard = "LITERAL_AUDIENCE_SCOPE", "COMPLETE_SCOPE_AND_RELATED_NORMS"
        elif any(definition_signal(retrieval_query, item["text"]) for item in evidence):
            reason, guard = "LITERAL_DEFINITION_CONTEXT", "DEFINITION_AND_SAME_SUBJECT_CONTEXT"
        elif literal_index is not None:
            reason, guard = "LITERAL_NORMATIVE_POLICY", "LITERAL_PERMISSION_PROHIBITION_OBLIGATION"
        else:
            reason, guard = "LITERAL_ENUMERATION_SOURCE", "COMPLETE_SOURCE_ENUMERATION"
        metadata.update(generation_guard=guard, complementary_source_policy="SAME_REVISION_SECTION_OR_EXPLICIT_SUBJECT", selected_parent_ranks=[i + 1 for i in related])
    scoped_procedure = source_scoped_procedure(retrieval_query, evidence) and not approved_card
    if scoped_procedure:
        mode, reason = "HANDOFF", "SOURCE_SCOPED_PROCEDURE_ONLY"
        answer_text = ("В найденном документе описан частный случай, применимый только при прямо указанных ниже условиях. "
                       "Общую процедуру для других ситуаций нужно уточнить у HR.\n\n"
                       + "\n\n".join(f"[{i}] {item['text']}" for i, item in enumerate(evidence, 1)))
        metadata["generation_guard"] = "EXPLICIT_CONDITIONAL_SOURCE_SCOPE"
    controlled_source = bool(related) or missing_index is not None or scoped_procedure
    try:
        from .local_ai import generation_policy
        active_generation_policy = generation_policy(db)
    except ImportError:
        active_generation_policy = None
    if MODE != "CORE" and active_generation_policy != "LOCAL_GENERATION" and not controlled_source:
        reason = "PROFILE_NOT_ACTIVATED" if active_generation_policy is None else "PROFILE_EXTRACTIVE_ONLY"
        metadata["generation_policy"] = active_generation_policy
    if MODE != "CORE" and active_generation_policy == "LOCAL_GENERATION" and not approved_card and not controlled_source and all(len(e["text"]) <= 5000 for e in evidence):
        generation_attempted = True
        generation_started = time.monotonic()
        try:
            request_budget = round(max(1.0, min(20.0, 26.5 - (time.monotonic() - pipeline_started))), 3)
            payload = {"question": retrieval_query, "evidence": evidence,
                       "max_new_tokens": 256, "history": [], "max_time_seconds": 17.0, "request_budget_seconds": request_budget,
                       "artifact_fingerprint": selected_model_fingerprints(db).get("generator")}
            metadata.update(generation_request_schema_version=pipeline_identities()["generation_request_schema_version"],
                            generation_request_payload_fingerprint=generation_payload_fingerprint(payload),
                            generation_request_budget_seconds=request_budget)
            generated = _request(GENERATOR + "/generate", payload, timeout=request_budget + 1.0)
            expected_generator = selected_model_fingerprints(db).get("generator")
            if not expected_generator or generated.get("artifact_fingerprint") != expected_generator:
                raise RuntimeError("DEGRADED_PROFILE_MISMATCH")
            abstained = model_abstained(generated.get("text", ""))
            valid, reason = (False, "MODEL_ABSTAINED") if abstained else validate_generated(generated.get("text", ""), evidence)
            metadata.update(generator_fingerprint=generated.get("artifact_fingerprint"),
                            output_tokens=generated.get("output_tokens"), input_tokens=generated.get("input_tokens"), verification=reason,
                            renderer_version=generated.get("renderer_version", "UNREPORTED"), renderer_fingerprint=generated.get("renderer_fingerprint"),
                            rendered_prompt_fingerprint=generated.get("rendered_prompt_fingerprint"), runtime_request_payload_fingerprint=generated.get("request_payload_fingerprint"))
            metadata["timings"].update(generation_queue_seconds=generated.get("queue_seconds"),
                generation_inference_seconds=generated.get("inference_seconds"), generation_runtime_request_seconds=generated.get("request_seconds"))
            if abstained:
                mode = "HANDOFF"
            elif valid:
                answer_text, mode = generated["text"], "LOCAL_GENERATED"
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", {})
            except (ValueError, AttributeError):
                detail = {}
            code = detail.get("code") if isinstance(detail, dict) else detail
            reason = code if code in {"GENERATION_BUDGET_EXCEEDED", "INCOMPLETE_GENERATION"} else "DEGRADED_PROFILE_MISMATCH" if exc.response.status_code == 409 else "LOCAL_GENERATOR_UNAVAILABLE"
            if isinstance(detail, dict):
                metadata["timings"].update(generation_queue_seconds=detail.get("queue_seconds"), generation_inference_seconds=detail.get("inference_seconds"), generation_runtime_request_seconds=detail.get("request_seconds"))
                metadata.update(input_tokens=detail.get("input_tokens"), output_tokens=detail.get("output_tokens"),
                    renderer_version=detail.get("renderer_version", "UNREPORTED"), renderer_fingerprint=detail.get("renderer_fingerprint"),
                    rendered_prompt_fingerprint=detail.get("rendered_prompt_fingerprint"), runtime_request_payload_fingerprint=detail.get("request_payload_fingerprint"))
            metadata["degraded"] = True
        except Exception as exc:
            reason = "DEGRADED_PROFILE_MISMATCH" if "PROFILE_MISMATCH" in str(exc) else "LOCAL_GENERATOR_UNAVAILABLE"
            metadata["degraded"] = True
        finally:
            metadata["timings"]["generation_request_seconds"] = round(time.monotonic() - generation_started, 6)
    if reason == "MODEL_ABSTAINED":
        metadata["timings"]["pipeline_seconds"] = round(time.monotonic() - pipeline_started, 6)
        return {"mode": "HANDOFF", "text": "В опубликованных документах недостаточно сведений для ответа на этот вопрос. Могу передать вопрос HR.",
                "citations": [], "evidence": [], "reason": reason, "provenance": metadata}
    if generation_attempted and mode == "EXTRACT":
        # The only relevant parent may be third in the retrieved set. Citation
        # presence alone does not make a two-parent fallback answer complete.
        answer_text = "Полные формулировки найденных документов:\n\n" + "\n\n".join(f"[{i}] {e['text']}" for i, e in enumerate(evidence, 1))
        metadata["fallback_source_policy"] = "ALL_COMPLETE_RETRIEVED_PARENTS"
    # Last-moment ACL/source revision recheck. Root outbox must call this gate
    # again at send time; the stored answer alone is not an authorization grant.
    current = _preview["permitted"] if _preview else published_corpus(db, employee)
    metadata["timings"]["pipeline_seconds"] = round(time.monotonic() - pipeline_started, 6)
    if any(c["revision_id"] not in current for c in citations):
        return {"mode": "HANDOFF", "text": "Документы обновились во время поиска. Повторите вопрос, пожалуйста.",
                "citations": [], "reason": "SOURCE_CHANGED", "provenance": metadata}
    source_footer = citation_footer(citations)
    if mode == "HANDOFF" and len(answer_text + source_footer) > 3500:
        prefix = "Полного текста указанного перечня среди найденных источников нет. " if reason == "REFERENCED_FULL_SOURCE_REQUIRED" else "Найденный документ описывает только частный случай. "
        return {"mode": "HANDOFF", "text": prefix + "Доступный фрагмент слишком большой для полного сообщения. Откройте источник и уточните вопрос у HR." + source_footer,
                "citations": citations, "evidence": evidence, "reason": "REFERENCED_SOURCE_TOO_LONG" if reason == "REFERENCED_FULL_SOURCE_REQUIRED" else "SCOPED_SOURCE_TOO_LONG", "provenance": metadata}
    if mode == "EXTRACT" and len(answer_text + source_footer) > 3500:
        return {"mode": "HANDOFF", "text": "Нашёл источники, но полные формулировки не помещаются в безопасный ответ. Уточните вопрос или обратитесь к HR." + source_footer,
                "citations": citations, "evidence": evidence, "reason": "EVIDENCE_TOO_LONG_FOR_SAFE_EXTRACT", "provenance": metadata}
    if len(answer_text + source_footer) > 3500:
        # Full extract cannot fit messenger cap. No partial normative statement.
        return {"mode": "HANDOFF", "text": "Нашёл применимые документы, но пункт слишком большой для безопасного краткого ответа. Уточните вопрос или обратитесь к HR." + source_footer,
                "citations": citations, "evidence": evidence, "reason": "PARENT_TOO_LARGE", "provenance": metadata}
    return {"mode": mode, "text": answer_text + source_footer, "citations": citations,
            "evidence": evidence, "provenance": {**metadata, "publication": "CANDIDATE" if _preview else "PUBLISHED", "candidate_snapshot_id": _preview.get("candidate_snapshot_id") if _preview else None}, "delivery_context": "EDITOR_PREVIEW" if _preview else "EMPLOYEE", "reason": reason}


def answer_still_authorized(db, conversation, answer):
    employee = current_employee(db, {"employee_id": conversation.data.get("employee_id")})
    if not isinstance(employee, Entity) or employee.data.get("enabled", True) is False:
        return False
    if answer.get("delivery_context") == "EDITOR_PREVIEW" or answer.get("provenance", {}).get("publication") == "CANDIDATE":
        return False
    permitted = published_corpus(db, employee)
    return all(c.get("revision_id") in permitted for c in answer.get("citations", []))


def preview_answer(db, actor, employee, question, candidate_snapshot_id=None):
    """Read-only HR preview of exact frozen candidate, never employee send data."""
    from fastapi import HTTPException
    from .common import require_role, assert_employee, get_entity
    from .knowledge import _revision, _extraction, _scope_access
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    assert_employee(db, actor, employee.id)
    conversation = Entity(id=new_id(), kind="conversation", data={"employee_id": employee.id, "preview": True}, status="ACTIVE")
    if not candidate_snapshot_id:
        result = answer_question(db, conversation, question, context=[])
        result["delivery_context"] = "EDITOR_PREVIEW"
        return result
    candidate = get_entity(db, candidate_snapshot_id, "knowledge_candidate")
    if candidate.status not in {"BUILT"}:
        raise HTTPException(409, "candidate_not_available_for_preview")
    head = db.scalar(select(Entity).where(Entity.kind == "knowledge_head").execution_options(populate_existing=True))
    if not head or candidate.data["base_snapshot_id"] != head.data["snapshot_id"]:
        raise HTTPException(409, "candidate_base_is_stale")
    base = get_entity(db, candidate.data["base_snapshot_id"], "knowledge_snapshot")
    changed_doc_ids = {pin["document_id"] for pin in candidate.data["members"].values()}
    permitted = {}
    for doc_id, revision_id in base.data.get("members", {}).items():
        if doc_id in changed_doc_ids:
            continue
        revision = get_entity(db, revision_id, "document_revision")
        doc = get_entity(db, doc_id, "document")
        if doc.status == "ACTIVE" and not doc.data.get("source_revoked") and scope_matches(doc.data.get("scope"), employee, db=db) and is_effective(revision.data):
            permitted[revision_id] = (doc, revision)
    for revision_id, pin in candidate.data["members"].items():
        revision, doc = _revision(db, actor, revision_id)
        if not _scope_access(actor, doc, write=True):
            raise HTTPException(404, "candidate_not_found")
        ext = _extraction(db, revision)
        if pin["content_fingerprint"] != revision.data["content_fingerprint"] or pin["extraction_revision_id"] != ext.id:
            raise HTTPException(409, "candidate_member_is_stale")
        if scope_matches(doc.data.get("scope"), employee, db=db):
            permitted[revision_id] = (doc, revision)
    preview = {"permitted": permitted, "changed_revision_ids": list(candidate.data["members"]),
               "chunk_ids": candidate.data["chunk_ids"], "candidate_snapshot_id": candidate.id}
    result = answer_question(db, conversation, question, context=[], _preview=preview)
    result["delivery_context"] = "EDITOR_PREVIEW"
    result["provenance"] = {**result.get("provenance", {}), "publication": "CANDIDATE", "candidate_snapshot_id": candidate.id}
    return result

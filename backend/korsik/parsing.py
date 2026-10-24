"""Bounded, offline document parsing. Input documents are data, never instructions.

Every physical page is inventoried before parsing. OCR words are evidence, not a
semantic interpretation of figures. Partial failures remain review blockers.
"""
from __future__ import annotations
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Callable

MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 500
MAX_PIXELS = 16_000_000
PDF_LOCK = threading.Lock()


def digest(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def block(kind, text, page_no, bbox=None, **extra):
    return {"id": str(uuid.uuid4()), "kind": kind, "text": text or "", "page_no": page_no,
            "bbox": bbox or [0, 0, 1, 1], "origin": extra.pop("origin", "NATIVE"),
            "review_status": "PENDING", "disposition": "INCLUDE", **extra}


def _ocr(image, language="rus+eng", psm=3):
    """Tesseract CLI with a hard deadline; no model download/network."""
    if image.width * image.height > MAX_PIXELS:
        image.thumbnail((4000, 4000))
    with tempfile.TemporaryDirectory(prefix="korsik-ocr-") as td:
        src = Path(td) / "page.png"
        image.convert("RGB").save(src)
        args = ["tesseract", str(src), "stdout", "-l", language, "--psm", str(psm), "tsv"]
        completed = subprocess.run(args, capture_output=True, timeout=55, check=True,
                                   env={**os.environ, "OMP_THREAD_LIMIT": "2"})
        lines = completed.stdout.decode("utf-8", "replace").splitlines()
        words, confidence = [], []
        for row in lines[1:]:
            parts = row.split("\t", 11)
            if len(parts) != 12 or not parts[11].strip():
                continue
            try:
                conf = float(parts[10])
            except ValueError:
                continue
            if conf >= 0:
                confidence.append(conf)
            words.append(parts[11])
        return " ".join(words), (sum(confidence) / len(confidence) if confidence else 0)


def _ruled_tables(image, page_no):
    """Recover explicit raster cell borders before a statistical layout model.

    This measures visible ruling lines; it does not invent columns from prose.
    Cells retain normalized page coordinates and always require human review.
    """
    import cv2
    import numpy as np
    gray = np.asarray(image.convert("L"))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(60, image.width // 25), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(60, image.height // 25))))
    contours, _ = cv2.findContours(cv2.bitwise_or(horizontal, vertical), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    added, boxes = [], []
    started = time.monotonic()
    def centers(indices):
        groups = []
        for value in indices:
            if not groups or value - groups[-1][-1] > 3:
                groups.append([int(value)])
            else:
                groups[-1].append(int(value))
        return [round(sum(group) / len(group)) for group in groups]
    for contour in sorted(contours, key=lambda c: cv2.boundingRect(c)[1])[:20]:
        x, y, w, h = cv2.boundingRect(contour)
        if w < image.width * .15 or h < 90:
            continue
        xs = centers(np.flatnonzero(np.count_nonzero(vertical[y:y+h, x:x+w], axis=0) > h * .65))
        ys = centers(np.flatnonzero(np.count_nonzero(horizontal[y:y+h, x:x+w], axis=1) > w * .65))
        if not (3 <= len(xs) <= 21 and 3 <= len(ys) <= 51) or (len(xs)-1)*(len(ys)-1) > 200:
            continue
        grid, confidences, cells = [], [], []
        for ri, (top, bottom) in enumerate(zip(ys, ys[1:])):
            row = []
            for ci, (left, right) in enumerate(zip(xs, xs[1:])):
                if time.monotonic() - started > 55:
                    raise ValueError("raster_table_deadline_exceeded")
                box = (x + left + 5, y + top + 5, x + right - 4, y + bottom - 4)
                text, confidence = _ocr(image.crop(box), psm=6)
                row.append(text)
                confidences.append(confidence)
                cells.append({"row": ri, "column": ci, "text": text, "bbox": [box[0]/image.width, box[1]/image.height, box[2]/image.width, box[3]/image.height]})
            grid.append(row)
        bbox = [x/image.width, y/image.height, (x+w)/image.width, (y+h)/image.height]
        boxes.append((x, y, x+w, y+h))
        added.append(block("TABLE", "\n".join(" | ".join(row) for row in grid), page_no, bbox,
                           grid=grid, cells=cells, origin="OPENCV_RULING_TESSERACT", confidence=round(sum(confidences)/len(confidences), 2),
                           issues=["TABLE_STRUCTURE_REVIEW"]))
    return added, boxes


def _save_preview(image, output_dir, page_no):
    image = image.convert("RGB")
    if image.width * image.height > MAX_PIXELS:
        image.thumbnail((4000, 4000))
    path = Path(output_dir) / f"page-{page_no:04d}.png"
    image.save(path)
    if sum(p.stat().st_size for p in Path(output_dir).glob("*.png")) > 500 * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise ValueError("derivative_budget_exceeded")
    return {"preview_file": path.name, "render_sha256": digest(path.read_bytes()),
            "width": image.width, "height": image.height}


def _pdf_inventory(path, out):
    import pypdfium2 as pdfium
    with PDF_LOCK:
        document = pdfium.PdfDocument(str(path))
        try:
            count = len(document)
            if count > MAX_PAGES:
                raise ValueError("page_limit_exceeded")
            return [{"page_no": n + 1, "status": "PENDING", "review_status": "PENDING", "issues": []} for n in range(count)]
        finally:
            document.close()


def _parse_pdf(path, output_dir, checkpoint, existing=None, page_options=None):
    import pypdfium2 as pdfium
    import pdfplumber
    pages = _pdf_inventory(path, output_dir)
    blocks = []
    started = time.monotonic()
    checkpoint(pages, blocks)
    with PDF_LOCK:
        document = pdfium.PdfDocument(str(path))
        try:
            with pdfplumber.open(path) as plumber:
                for row in pages:
                    n = row["page_no"]
                    if n in (existing or set()):
                        row.update(status="RETAINED", retained_from_previous_attempt=True)
                        checkpoint(pages, blocks)
                        continue
                    if time.monotonic() - started > 600:
                        row["status"] = "FAILED"
                        row["issues"] = ["ATTEMPT_DEADLINE"]
                        checkpoint(pages, blocks)
                        continue
                    pdfpage, textpage, bitmap = None, None, None
                    try:
                        pdfpage = document[n - 1]
                        width, height = pdfpage.get_size()
                        scale = min(300 / 72, (MAX_PIXELS / max(width * height, 1)) ** .5)
                        bitmap = pdfpage.render(scale=scale)
                        image = bitmap.to_pil().copy()
                        options = (page_options or {}).get(n, {})
                        rotation = options.get("rotation", 0)
                        if rotation:
                            image = image.rotate(-rotation, expand=True)
                        row["rotation_degrees"] = rotation
                        row.update(_save_preview(image, output_dir, n))
                        textpage = pdfpage.get_textpage()
                        native = textpage.get_text_range().strip()
                        if options.get("force_ocr") or rotation:
                            native = ""
                        printable = sum(c.isalnum() for c in native)
                        if printable >= 30:
                            p = plumber.pages[n - 1]
                            # pdfplumber coordinates use top-left origin. Native text kept
                            # whole-page so conditions and exceptions are never cropped.
                            blocks.append(block("TEXT", native, n, origin="NATIVE_PDF"))
                            tables = p.find_tables()
                            if tables:
                                boxes = [table.bbox for table in tables]
                                def outside_tables(obj):
                                    if obj.get("object_type") != "char":
                                        return True
                                    x, y = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
                                    return not any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in boxes)
                                non_table_text = p.filter(outside_tables).extract_text() or ""
                                current_text = blocks[-1]
                                current_text["text"] = non_table_text
                                current_text["table_regions_excluded"] = True
                                if not non_table_text.strip():
                                    current_text["disposition"] = "REFERENCE_ONLY"
                            for table in tables[:30]:
                                grid = [[str(v or "") for v in r] for r in table.extract()]
                                bbox = [table.bbox[0] / width, table.bbox[1] / height, table.bbox[2] / width, table.bbox[3] / height]
                                blocks.append(block("TABLE", "\n".join(" | ".join(r) for r in grid), n, bbox,
                                                    grid=grid, origin="PDFPLUMBER", issues=["TABLE_STRUCTURE_REVIEW"]))
                            for picture in p.images[:30]:
                                if picture.get("width", 0) * picture.get("height", 0) < 2000:
                                    continue
                                bbox = [picture.get("x0", 0) / width, picture.get("top", 0) / height,
                                        picture.get("x1", width) / width, picture.get("bottom", height) / height]
                                blocks.append(block("FIGURE", "", n, bbox, issues=["FIGURE_DESCRIPTION_REQUIRED"],
                                                    image_sha256=row["render_sha256"]))
                        else:
                            text, confidence = _ocr(image)
                            row["ocr_confidence"] = round(confidence, 2)
                            if text:
                                blocks.append(block("TEXT", text, n, origin="TESSERACT", confidence=confidence,
                                                    issues=["OCR_CONTENT_REVIEW"]))
                            else:
                                row["issues"].append("NO_READABLE_TEXT")
                            row["issues"].append("SCANNED_PAGE_REVIEW")
                        if os.environ.get("PARSER_PROFILE", "cpu_basic") == "cpu_structure":
                            # Plain, legible scans take OCR only. Explicit borders,
                            # poor OCR, sparse diagram-like pages, and operator-selected
                            # pages justify the slower layout/table model.
                            ruled, ruled_boxes = _ruled_tables(image, n) if printable < 30 else ([], [])
                            candidate_tables = [b for b in blocks if b["page_no"] == n and b["kind"] == "TABLE"]
                            manual_pages = {int(v) for v in os.environ.get("PARSER_STRUCTURE_PAGES", "").split(",") if v.strip().isdigit()}
                            suspicious_scan = printable < 30 and (row.get("ocr_confidence", 100) < 60 or len(text or "") < 80)
                            if ruled or candidate_tables or suspicious_scan or n in manual_pages or len(plumber.pages[n - 1].lines) > 15:
                                if width * height * (300 / 72) ** 2 > MAX_PIXELS:
                                    raise ValueError("structure_render_pixel_budget_exceeded")
                                if rotation:
                                    # Docling's original-PDF boxes do not describe
                                    # this rotated raster. Use local OCR/measured
                                    # ruling and require HR review of this result.
                                    structured, diagnostics = [], {"status": "SUCCESS_OPERATOR_ROTATED_OCR", "model_inference_used": False}
                                else:
                                    structured, diagnostics = _structural_page(path, n, width, height, row["render_sha256"])
                                if ruled:
                                    from PIL import ImageDraw
                                    outside = image.copy()
                                    draw = ImageDraw.Draw(outside)
                                    for rectangle in ruled_boxes:
                                        draw.rectangle(rectangle, fill="white")
                                    outside_text, outside_confidence = _ocr(outside)
                                    blocks = [b for b in blocks if not (b["page_no"] == n and b["kind"] in {"TABLE", "TEXT"})]
                                    if outside_text:
                                        blocks.append(block("TEXT", outside_text, n, origin="TESSERACT", confidence=outside_confidence, table_regions_excluded=True))
                                    blocks.extend(ruled)
                                    # Visible raster ruling has a measured grid. Keep
                                    # model diagnostics, never replace it with a 1x1 guess.
                                    structured = [b for b in structured if b["kind"] == "FIGURE" and not any(
                                        b["bbox"][0] < t["bbox"][2] and b["bbox"][2] > t["bbox"][0] and
                                        b["bbox"][1] < t["bbox"][3] and b["bbox"][3] > t["bbox"][1] for t in ruled)]
                                    diagnostics["table_route"] = "VISIBLE_RULING_TESSERACT"
                                elif candidate_tables:
                                    structured = [b for b in structured if b["kind"] == "FIGURE"]
                                    diagnostics["table_route"] = "NATIVE_PDFPLUMBER"
                                elif any(b["kind"] == "TABLE" for b in structured):
                                    blocks = [b for b in blocks if not (b["page_no"] == n and b["kind"] in {"TABLE", "TEXT"})]
                                else:
                                    # Native/OCR whole-page text remains authoritative
                                    # when the structure model found no table.
                                    structured = [b for b in structured if b["kind"] != "TEXT"]
                                blocks.extend(structured)
                                row["structure_diagnostics"] = diagnostics
                                if "SUCCESS" not in diagnostics["status"].upper():
                                    row["issues"].append("STRUCTURE_INCOMPLETE_MANUAL_REVIEW")
                            else:
                                row["structure_route"] = "OCR_PLAIN_TEXT" if printable < 30 else "NATIVE_TEXT"
                        row["status"] = "PROCESSED"
                    except Exception as exc:
                        row["status"] = "FAILED"
                        row["issues"].append("PARSE_ERROR")
                        row["error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
                    finally:
                        if textpage is not None:
                            textpage.close()
                        if bitmap is not None:
                            bitmap.close()
                        if pdfpage is not None:
                            pdfpage.close()
                        checkpoint(pages, blocks)
        finally:
            document.close()
    return pages, blocks


def _parse_pptx(path, output_dir, checkpoint, existing=None):
    from pptx import Presentation
    from PIL import Image
    with zipfile.ZipFile(path) as zipped:
        infos = zipped.infolist()
        if len(infos) > 10000 or sum(i.file_size for i in infos) > 500 * 1024 * 1024:
            raise ValueError("archive_expansion_limit")
        if any(i.file_size > 100 * 1024 * 1024 for i in infos):
            raise ValueError("archive_member_limit")
    started = time.monotonic()
    image_ocr_cache = {}
    prs = Presentation(path)
    rendered_pdf = None
    try:
        import shutil
        if shutil.which("libreoffice"):
            render_dir = Path(output_dir) / "rendered"
            render_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="korsik-office-profile-") as profile:
                config = Path(profile) / "user"
                config.mkdir()
                (config / "registrymodifications.xcu").write_text('<?xml version="1.0" encoding="UTF-8"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>')
                subprocess.run(["libreoffice", "-env:UserInstallation=" + Path(profile).as_uri(), "--headless", "--nologo", "--nodefault", "--norestore", "--nofirststartwizard", "--convert-to", "pdf", "--outdir", str(render_dir), str(path)],
                               capture_output=True, timeout=120, check=True)
            candidate = render_dir / (Path(path).stem + ".pdf")
            if candidate.exists() and candidate.stat().st_size < 100 * 1024 * 1024:
                rendered_pdf = candidate
    except (OSError, subprocess.SubprocessError):
        # Source PPTX remains reviewable; render failure is surfaced, not hidden.
        rendered_pdf = None
    if len(prs.slides) > MAX_PAGES:
        raise ValueError("page_limit_exceeded")
    pages = [{"page_no": i + 1, "status": "PENDING", "review_status": "PENDING", "issues": []} for i in range(len(prs.slides))]
    blocks = []
    checkpoint(pages, blocks)
    for row, slide in zip(pages, prs.slides):
        n = row["page_no"]
        if n in (existing or set()):
            row.update(status="RETAINED", retained_from_previous_attempt=True)
            checkpoint(pages, blocks)
            continue
        page_started = time.monotonic()
        if page_started - started > 600:
            row["status"] = "FAILED"
            row["issues"] = ["ATTEMPT_DEADLINE"]
            checkpoint(pages, blocks)
            continue
        try:
            def visit(shapes):
                for shape in shapes:
                    if time.monotonic() - page_started > 60:
                        raise TimeoutError("PAGE_DEADLINE")
                    bbox = [shape.left / prs.slide_width, shape.top / prs.slide_height,
                            (shape.left + shape.width) / prs.slide_width, (shape.top + shape.height) / prs.slide_height]
                    if shape.has_text_frame:
                        text = "\n".join(p.text for p in shape.text_frame.paragraphs).strip()
                        if text:
                            blocks.append(block("TEXT", text, n, bbox, origin="PPTX_TEXT"))
                    if shape.has_table:
                        grid = [[cell.text for cell in r.cells] for r in shape.table.rows]
                        blocks.append(block("TABLE", "\n".join(" | ".join(r) for r in grid), n, bbox,
                                            grid=grid, origin="PPTX_TABLE", issues=["TABLE_STRUCTURE_REVIEW"]))
                    if shape.shape_type == 13:  # picture
                        image_bytes = shape.image.blob
                        image = Image.open(io.BytesIO(image_bytes))
                        if image.width * image.height > 30_000_000:
                            raise ValueError("decoded_image_limit")
                        image_hash = digest(image_bytes)
                        ocr_text = image_ocr_cache.get(image_hash, "")
                        if image_hash not in image_ocr_cache:
                            try:
                                ocr_text, _ = _ocr(image)
                            except (OSError, subprocess.SubprocessError):
                                pass
                            image_ocr_cache[image_hash] = ocr_text
                        blocks.append(block("FIGURE", "", n, bbox, origin="PPTX_IMAGE", ocr_labels=ocr_text,
                                            image_sha256=digest(image_bytes), issues=["FIGURE_DESCRIPTION_REQUIRED"]))
                    if shape.shape_type == 6:
                        visit(shape.shapes)
                    if "graphicFrame" in shape._element.tag and not shape.has_table:
                        labels = []
                        for element in shape._element.iter():
                            if element.tag.endswith("}relIds"):
                                rid = element.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}dm")
                                if rid:
                                    try:
                                        from lxml import etree
                                        xml = etree.fromstring(slide.part.related_part(rid).blob, parser=etree.XMLParser(resolve_entities=False, no_network=True))
                                        labels.extend(xml.xpath("//*[local-name()='t']/text()"))
                                    except Exception:
                                        pass
                        blocks.append(block("FIGURE", "", n, bbox, origin="PPTX_SMARTART_OR_CHART", ocr_labels=" ".join(labels),
                                            image_sha256=digest(shape._element.xml.encode()), issues=["FIGURE_DESCRIPTION_REQUIRED"]))
                        row["issues"].append("SMARTART_OR_CHART_MANUAL_REVIEW")
            visit(slide.shapes)
            row["status"] = "PROCESSED"
            # PPTX extraction is structural, not a renderer. Explicitly no fake
            # canonical slide image. Source PPTX remains available for review.
            row["preview_available"] = False
            if rendered_pdf:
                import pypdfium2 as pdfium
                with PDF_LOCK:
                    pdf = pdfium.PdfDocument(str(rendered_pdf))
                    page = bitmap = None
                    try:
                        page = pdf[n - 1]
                        width, height = page.get_size()
                        scale = min(180 / 72, (MAX_PIXELS / max(width * height, 1)) ** .5)
                        bitmap = page.render(scale=scale)
                        row.update(_save_preview(bitmap.to_pil().copy(), output_dir, n))
                        row["preview_available"] = True
                        row["render_engine"] = "LIBREOFFICE_PDFIUM"
                        for figure in blocks:
                            if figure["page_no"] == n and figure["origin"] == "PPTX_SMARTART_OR_CHART":
                                # Hash of canonical visual page + exact graph XML,
                                # because labels alone do not pin layout/connectors.
                                figure["image_sha256"] = digest((row["render_sha256"] + figure["image_sha256"]).encode())
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        if page is not None:
                            page.close()
                        pdf.close()
            if not row["preview_available"]:
                row["preview_reason"] = "PPTX_SOURCE_REVIEW_REQUIRED"
                row["issues"].append("PPTX_SOURCE_REVIEW_REQUIRED")
        except Exception as exc:
            row["status"] = "FAILED"
            row["issues"].append("PARSE_ERROR")
            row["error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
        checkpoint(pages, blocks)
    return pages, blocks


def parse_document(path, output_dir, checkpoint: Callable | None = None, *, skip_pages=None, page_options=None):
    """Return {pages,blocks,...}; callback permits durable partial checkpoints."""
    path, output_dir = Path(path), Path(output_dir)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("file_limit_exceeded")
    output_dir.mkdir(parents=True, exist_ok=True)
    callback = checkpoint or (lambda pages, blocks: None)
    if not isinstance(skip_pages, (type(None), list, set, tuple)) or any(type(n) is not int or not 1 <= n <= MAX_PAGES for n in (skip_pages or [])):
        raise ValueError("invalid_retained_pages")
    retained = set(skip_pages or [])
    if not isinstance(page_options, (dict, type(None))) or len(page_options or {}) > MAX_PAGES:
        raise ValueError("invalid_page_options")
    options = {}
    for key, value in (page_options or {}).items():
        if not str(key).isdigit() or not 1 <= int(key) <= MAX_PAGES or not isinstance(value, dict) or set(value) - {"rotation", "force_ocr"}:
            raise ValueError("invalid_page_options")
        if type(value.get("rotation", 0)) is not int or value.get("rotation", 0) not in {0, 90, 180, 270} or type(value.get("force_ocr", False)) is not bool:
            raise ValueError("invalid_page_options")
        options[int(key)] = value
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            pages, blocks = _parse_pdf(path, output_dir, callback, retained, options)
        except Exception as exc:
            if "password" in str(exc).lower() or "encrypted" in str(exc).lower():
                raise ValueError("password_protected_source") from exc
            raise
    elif suffix == ".pptx":
        pages, blocks = _parse_pptx(path, output_dir, callback, retained)
    elif suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8-sig") if 1 not in retained else ""
        pages = [{"page_no": 1, "status": "RETAINED" if 1 in retained else "PROCESSED", "review_status": "PENDING", "issues": []}]
        blocks = [block("TEXT", text, 1, origin="UTF8_TEXT")] if 1 not in retained else []
        callback(pages, blocks)
    elif suffix in {".png", ".jpg", ".jpeg"}:
        if 1 in retained:
            pages, blocks = [{"page_no": 1, "status": "RETAINED", "review_status": "PENDING", "issues": []}], []
            callback(pages, blocks)
            return {"pages": pages, "blocks": blocks, "source_sha256": digest(path.read_bytes()), "parser_version": "korsik-parser-1", "status": "REVIEW_REQUIRED"}
        from PIL import Image
        with Image.open(path) as image:
            if image.width * image.height > 30_000_000:
                raise ValueError("decoded_image_limit")
            rotation = options.get(1, {}).get("rotation", 0)
            if rotation:
                image = image.rotate(-rotation, expand=True)
            page = {"page_no": 1, "status": "PROCESSED", "review_status": "PENDING", "issues": ["IMAGE_CONTENT_REVIEW"]}
            page["rotation_degrees"] = rotation
            page.update(_save_preview(image, output_dir, 1))
            text, conf = _ocr(image)
        pages = [page]
        blocks = [block("TEXT", text, 1, origin="TESSERACT", confidence=conf)] if text else []
        if not text:
            pages[0]["issues"].append("NO_READABLE_TEXT")
        callback(pages, blocks)
    else:
        raise ValueError("unsupported_media_type")
    return {"pages": pages, "blocks": blocks, "source_sha256": digest(path.read_bytes()),
            "parser_version": "korsik-parser-1", "content_fingerprint": digest({"pages": pages, "blocks": blocks}),
            "status": "PARTIAL_FAILURE" if any(p["status"] == "FAILED" for p in pages) else "REVIEW_REQUIRED"}


STRUCTURAL_CONVERTER = None
STRUCTURAL_INFO = {"status": "NOT_SELECTED"}
STRUCTURAL_LOCK = threading.Lock()


def load_structural_parser():
    """Prewarm pinned Heron/TableFormer FAST on CPU, strictly local artifacts."""
    global STRUCTURAL_CONVERTER, STRUCTURAL_INFO
    if STRUCTURAL_CONVERTER is not None:
        return STRUCTURAL_INFO
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableStructureOptions, TableFormerMode, TesseractCliOcrOptions, LayoutObjectDetectionOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    from docling.datamodel.object_detection_engine_options import TransformersObjectDetectionEngineOptions
    from docling.datamodel.stage_model_specs import ObjectDetectionModelSpec
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    import torch
    torch.set_num_threads(2)
    root = Path(os.environ.get("DOCLING_ARTIFACTS_PATH", "/models/docling"))
    verified = []
    for folder, revision in [("docling-project--docling-layout-heron", "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"),
                             ("docling-project--docling-models", "2199320848bb9a8a519d22e4b528185a4f9a6f64")]:
        base = root / folder
        manifest = json.loads((base / "manifest.json").read_text())
        if manifest.get("revision") != revision:
            raise RuntimeError("Docling artifact revision mismatch")
        for record in manifest["files"]:
            local = (base / record["path"]).resolve()
            if not local.is_relative_to(base.resolve()) or not local.is_file() or local.stat().st_size != record["bytes"] or digest(local.read_bytes()) != record["sha256"]:
                raise RuntimeError("Docling artifact inventory mismatch")
        verified.append({"repository": manifest["repository"], "revision": revision, "files": len(manifest["files"])})
    options = PdfPipelineOptions(
        artifacts_path=root, enable_remote_services=False, allow_external_plugins=False,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=2),
        do_ocr=True, ocr_options=TesseractCliOcrOptions(lang=["rus", "eng"], psm=3, scale=300 / 72),
        do_table_structure=True, table_structure_options=TableStructureOptions(mode=TableFormerMode.FAST, do_cell_matching=True),
        layout_options=LayoutObjectDetectionOptions(
            model_spec=ObjectDetectionModelSpec(name="korsik-heron", repo_id="docling-project/docling-layout-heron", revision="8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"),
            engine_options=TransformersObjectDetectionEngineOptions(torch_dtype="float32", compile_model=False)),
        do_picture_description=False, do_picture_classification=False, do_code_enrichment=False,
        do_formula_enrichment=False, do_chart_extraction=False, generate_page_images=False,
        generate_picture_images=False, document_timeout=55,
        ocr_batch_size=1, layout_batch_size=1, table_batch_size=1)
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF], format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options, backend=PyPdfiumDocumentBackend)})
    started = time.monotonic()
    converter.initialize_pipeline(InputFormat.PDF)
    STRUCTURAL_CONVERTER = converter
    STRUCTURAL_INFO = {"status": "READY", "profile": "cpu_structure", "runtime": "torch-fp32-cpu", "torch_version": torch.__version__,
                       "docling_version": "2.142.1", "mode": "TableFormer_FAST", "artifacts": verified,
                       "load_seconds": round(time.monotonic() - started, 3), "remote_services": False, "vision_generation": False}
    return STRUCTURAL_INFO


def _structural_page(path, page_no, width, height, render_hash):
    """One-page inference keeps original page coordinates/provenance explicit."""
    load_structural_parser()
    with STRUCTURAL_LOCK:
        started = time.monotonic()
        result = STRUCTURAL_CONVERTER.convert(Path(path), page_range=(page_no, page_no), raises_on_error=False)
        added = []
        ordered_text = []
        for item, level in result.document.iterate_items():
            label = str(getattr(item, "label", "")).lower()
            if not ("table" in label or "picture" in label):
                text = str(getattr(item, "text", "")).strip()
                if text:
                    ordered_text.append(text)
                continue
            prov = next(iter(getattr(item, "prov", [])), None)
            if not prov:
                continue
            box = prov.bbox
            top, bottom = box.t, box.b
            if "BOTTOMLEFT" in str(box.coord_origin).upper():
                top, bottom = height - box.t, height - box.b
            bbox = [max(0, min(1, box.l / width)), max(0, min(1, min(top, bottom) / height)),
                    max(0, min(1, box.r / width)), max(0, min(1, max(top, bottom) / height))]
            if "table" in label:
                data = item.data
                grid = [["" for _ in range(data.num_cols)] for _ in range(data.num_rows)]
                for cell in data.table_cells:
                    row, column = cell.start_row_offset_idx, cell.start_col_offset_idx
                    if row < len(grid) and column < len(grid[row]):
                        grid[row][column] = str(cell.text)
                if grid:
                    added.append(block("TABLE", "\n".join(" | ".join(r) for r in grid), page_no, bbox,
                                       grid=grid, table=data.model_dump(mode="json"), origin="DOCLING_TABLEFORMER_FAST",
                                       issues=["TABLE_STRUCTURE_REVIEW"], runtime=STRUCTURAL_INFO))
            elif "picture" in label:
                added.append(block("FIGURE", "", page_no, bbox, origin="DOCLING_HERON",
                                   image_sha256=digest((render_hash + json.dumps(bbox)).encode()),
                                   issues=["FIGURE_DESCRIPTION_REQUIRED"], runtime=STRUCTURAL_INFO))
        if ordered_text:
            added.insert(0, block("TEXT", "\n".join(ordered_text), page_no, origin="DOCLING_TESSERACT_READING_ORDER", table_regions_excluded=True))
        return added, {"status": str(result.status), "seconds": round(time.monotonic() - started, 3), "runtime": STRUCTURAL_INFO}

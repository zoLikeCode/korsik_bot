# Local model and parsing services

These images never download artifacts at startup. Prepare immutable pinned files
on a connected build host with `python fetch_models.py --target /path/to/models`;
transfer that directory into the private contour. Each role verifies every
`manifest.json` path, size and SHA-256 before loading. Readiness reflects a loaded
model, not just a listening process. License/notice files travel in the inventory.

Build from the application root:

```sh
docker build -t korsik-models:cpu -f services/model_server/Dockerfile .
docker build -t korsik-generator:local -f services/model_server/Dockerfile.generation .
docker build -t korsik-parser:local -f services/model_server/Dockerfile.parser .
docker build -t korsik-parser:structure -f services/model_server/Dockerfile.parser.structure .
```

CPU roles use `MODEL_ROLE=embedder|reranker`, `MODEL_DIR=/models/<role>`.
Granite97 uses the pinned FP32 ONNX checkpoint, CLS pooling and L2 normalization.
Horizon consumes complete bounded query/document pairs; an oversized input fails
rather than silently truncating normative evidence. `/chunks` can be called on
both tokenizers so each searchable child fits both model budgets. Model changes
require a new corpus projection; the backend rejects a fingerprint mismatch.

The primary generator role loads the official **Qwen3.5-4B BF16** checkpoint through
Transformers on one CUDA GPU. The API accepts **text only**, but the full upstream
multimodal checkpoint is loaded; this is not a claim that vision weights were
removed. It disables thinking through the chat template, serializes inference,
bounds history to six messages / 800 tokenizer tokens and total context to 4096
(input <=3840, output <=256). An incomplete generation returns an error; the
backend then uses an applicable complete extract or human handoff. Mechanical
citation, number and contact checks are not semantic proof of truth.
The generation admission queue permits one running request and at most eight
waiting. Overflow returns 429. Decode has a maximum 17-second cooperative
budget; admission, prompt preparation and inference share a request budget of
at most 22 seconds (20 by default). The backend reduces this budget using time
already spent retrieving sources and bounds its HTTP wait separately. A GPU
step can overrun a cooperative decode limit; the late-answer fence still applies.
Controlled `GENERATION_BUDGET_EXCEEDED` and `INCOMPLETE_GENERATION` responses
include phase, queue/inference/request timings and prompt fingerprints. A failed
generation returns all complete evidence parents if the whole response fits;
otherwise it explicitly hands off, preserving source access.

New profiles pin chunker/retrieval code and the actual generation runtime source.
The generator reports its deployed source SHA256 and the SHA256 of the exact
rendered prompt. A changed pipeline invalidates old QA/profile identities; a
passing review must also match current retrieval and generator source hashes.
Legacy unversioned chunks are labelled as such rather than assigned a guessed
historical code version.

Default preparation installs `generator-4b` (revision
`851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`). Both official weight shard hashes are
pinned in `fetch_models.py`; the complete artifact inventory has fingerprint
`1fe278d9e50f5fe426c80efe3845911a3bbcc057d1f975c660a8c2f39c6d21d0`.
Set `MODEL_DIR=/models/generator-4b`, container memory limit `12g`, and give it one
12GB CUDA GPU. A real RTX3060 probe sampled 9574MiB VRAM and 1.339GiB container RSS
during the application corpus evaluation. This is a measured configuration, not a
claim that any CPU or GPU can run it at the same speed.

The 2B checkpoint is an optional download with `--role generator-2b` (revision
`15852e8c16360a2fea060d615a32b45270f8a8fc`, weight SHA-256
`aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1`).
The 0.8B variant remains an explicit optional
download with `--role generator`; it **failed semantic review** on two of four
short supplied-excerpt cases, including a contradictory denial of required visitor
permission. The same four cases passed implementation review with 2B, with one
awkward Russian opening sentence. These four cases establish neither whole-corpus
RAG acceptance nor company HR approval. Each artifact's `quality-report.json`
records the exact inputs, outputs, reviewer and findings; activation also binds
the runtime to the selected manifest fingerprint.
Manual review of the actual published corpus subsequently found a **2B semantic
contradiction about monetary gifts**, despite mechanically valid citations. The
raw failure remains in `tests/policies-rag-evaluation-final.json`. Direct questions
about permissions, prohibitions or duties with applicable prohibition evidence
therefore bypass generation and return the complete canonical parent literally
(`LITERAL_PERMISSION_PROHIBITION_OBLIGATION`), including its conditions. The
four-case model PASS is explicitly limited to those four inputs; it does not
override this known weakness.

The selected 4B model passed manual review on eight identical oracle-source
inputs, versus five for 2B. Raw 4B generation still failed two of eight actual
retrieval-context cases; those failures remain in
`tests/model-quality/qwen-4b-comparison.json`. The final guarded application rerun
covered twenty synthetic policy questions: seven grounded generated answers,
eleven complete extracts and two handoffs for unavailable full appendices. A
conflict-of-interest answer quotes only the found business-gift scenario; it does
not establish a complete procedure for all conflicts. The set was used to repair
general pipeline defects, so it is an implementation regression set, not an
untouched benchmark or company HR acceptance. Single-active pipeline latency was
p50 7.919s / nearest-rank p95 23.662s / max 24.093s in the fresh release10
17-second-budget rerun; HTTP/load acceptance is measured separately. The current
scoped review is `tests/model-quality/qwen-4b-release10.json`, pinned to both
retrieval and generation runtime source hashes. The older `qwen-4b.json` and raw
model failures remain historical evidence; they cannot activate a changed
pipeline. The current review permits this guarded scope only. Literal normative/list guards,
current source/ACL checks and human knowledge publication review remain required.

The isolated parser has no DB credentials. It accepts a bounded multipart upload
at `/parse` and streams NDJSON per-page checkpoints including source previews.
The backend persists every checkpoint, including a partial failure. Retry sends
only retained physical page numbers in the optional multipart `skip_pages` JSON
list; the parser inventories all pages but skips OCR/rendering for those pages.
Their reviewed text, block IDs and previews stay in the fenced backend checkpoint,
so a long document can advance beyond an attempt deadline instead of repeatedly
processing the same first pages. It uses
PDFium, pdfplumber, Tesseract rus+eng, native PPTX extraction and LibreOffice's
headless PDF render for PPTX previews. SmartArt labels are marked as visual
evidence, not interpreted as a procedure. Tables and figures require HR review.
The `cpu_structure` image adds pinned local Docling Heron + TableFormer FAST on
CPU. A measured automatic route keeps legible plain scans on Tesseract, and calls
structure inference for poor OCR, sparse pages, visible tables or explicit operator
selection. Native PDF grids remain pdfplumber's source-backed grids. Raster ruled
tables use OpenCV morphology to measure visible borders and Tesseract per cell;
this prevents a model's incorrect 1×1 table guess from replacing a visible 3×3
grid. Borderless model tables remain subject to HR review. On the actual AVX-only
host all 23 scanned PVTР pages parsed locally; all three supplied PPTX files produced
33 native-text slides and 33 real LibreOffice previews. An offline, cold table
probe verified the 3×3 cell grid and numeric cells. OCR misread the short Latin
labels HR/IT, so human review remains mandatory; no perfect OCR claim is made.

Feedback fixture model: `feedback/model.json` is TF-IDF + one-vs-rest logistic
regression trained on **40 explicitly synthetic examples**, with 10 disjoint
synthetic held-out examples. The reported fixture score does not establish
company-domain quality. All suggestions require HR review. No automatic hiring,
probation, disciplinary or dismissal decision is authorized by this artifact.
Retrain on human-labeled company data through:

```sh
python -m korsik.analysis train --dataset /path/labeled.jsonl --output /models/feedback/model.json
```

Runtime containers should use internal networks, read-only model mounts,
non-root users and bounded CPU/RAM/PID/tmpfs. MAX credentials belong only in the
transport service. Image construction and operator artifact preparation may use
the internet; runtime inference/parsing must work with egress denied.

Lexical recall uses pinned Snowball3.1.1 Russian stemming on the local DB corpus,
including numbered appendices. This adds a third ACL-filtered recall branch to
the existing dense and wire sparse branches, then diversifies candidates to one
canonical page parent before bounded reranking. The sparse wire vocabulary is
unchanged, so existing vectors remain compatible. Enumeration questions preserve
complete source lists; requests for a full list whose found parent only references
an unavailable appendix are handed to HR with that source citation. Neither rule
invents the missing appendix.

Selected OCR retry accepts page_numbers and optional per-page rotation (clockwise
0/90/180/270) / force_ocr for PDF and raster inputs. All other physical pages and
reviewed blocks are retained. Manual corrections supersede matching machine
regions and force a new page review; changed visual renders preserve old authored
descriptions as reference-only pending HR inspection. Rotated rasters never reuse
Docling boxes inferred on the original orientation.

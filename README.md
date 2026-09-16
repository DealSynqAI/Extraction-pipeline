# DealSynq OCR pipeline through Unified Source Blocks

Current package version: `0.7.0`.

This folder implements the requested architecture boundary:

`PDF -> ingestion -> inspection -> page/region routing -> typed extraction -> reconstruction/validation -> Unified Source Block Collection`

It deliberately performs no semantic entity, fact, relation, embedding, retrieval, or DeepSeek work.

## What is implemented

- Immutable run directories, stable document IDs, SHA-256 source identity, and a byte-identical preserved source copy.
- Deterministic PDF inspection with native character quality, positioned text, image coverage, vector counts, table candidates, page dimensions, and rotation.
- Region-level routing for mixed pages. Low-confidence image regions can be sent to Qwen3-VL for classification; confidence does not bypass later semantic processing.
- Native-text usability routing. A text layer is used only when it passes the configured quality threshold; otherwise RapidOCR supplies visible text and coordinates.
- Native table parsing into one hierarchical table block with owned columns, rows, typed cells, cell coordinates, and cell evidence IDs. When native structure is unavailable, the result stays `needs_review`.
- One-row PDF "tables" over comparison-card backgrounds are reclassified using positioned words: repeated bullet anchors define text lanes, heading spans define category/offer hierarchy, and each claim keeps its own coordinates and evidence IDs. Percent mentions stay attached to the owning claim and preserve ranges. A weak layout or a conflicting/missing Qwen comparison review remains `needs_review`; this is not a document- or page-specific rule.
- Logical visual-object merging before classification, including fragmented PDF chart images and repeated page-band decoration detection.
- PDF-object completeness detection for vector bar clusters and dense raster point/label clusters. Incomplete bar and scatterplot reconstructions are forced to `needs_review` instead of silently passing as text.
- Tables, charts, and maps always use a sequential specialist flow when Qwen is enabled: deterministic native/OCR reading, OpenCV or table geometry, Qwen3-VL semantic linking/review, then Python reconstruction and validation. Deterministic evidence is retained when the model fails or disagrees.
- Generic reconstruction of rotated native-PDF chart values plus x-aligned bar ownership. This recovers vertical percentage labels without page-number or document-value hardcoding.
- Pie-chart label/value pairs are checked against OCR evidence and Qwen confirmation. When a PDF exposes distinct soft-mask wedge objects whose areas reconcile one-to-one with the visible percentages, each observation references its exact PDF mask; otherwise slice geometry is explicitly unresolved and the chart remains in review. Merged image-fragment bounding boxes alone never count as slice ownership.
- US-state choropleth values are checked against a reusable Census boundary reference. The pipeline compares conterminous-US projections, requires strong rendered-silhouette alignment, associates interior OCR labels with state polygons and external labels with OpenCV leader lines, and keeps Qwen as the mandatory semantic stage without letting an unverified Qwen owner replace geometry. The scale endpoints are not state observations; map credits and unresolved values remain explicit. State names inferred from reference geometry are distinguished from names actually printed on the page. Maps that do not fit the reference remain in review.
- Native positioned financial-table reconstruction that infers columns from repeated numeric alignment, recovers headers and sections from geometry, and reconciles generic subtotals against the final total.
- Raw OpenCV arrays are isolated in hashed `diagnostics/vision-features/*.json` artifacts. Source blocks expose only compact `vision_summary`, `vision_features_ref`, and `region_image` fields.
- A common source-block envelope, explicit errors/warnings, provenance, confidence, normalized coordinates, native-versus-visible OCR agreement, page-level validation, and a complete OCR evidence ledger/disposition list.
- Explicit page hierarchy on every block (`parent_block_id`, `child_block_ids`, depth, and heading level), plus a semantic role distinct from the coarse block type.
- General whitespace segmentation for image-backed text panels, so logos, panel headings, and body/legal copy are not flattened into one giant text value.
- Evidence-aware brand-mark recognition based on short repeated page text; chart-like geometry alone can no longer force a logo to remain an unresolved visual.
- A final unassigned-evidence recovery pass that can create OCR-backed brand marks even when the PDF object layer omitted the logo region.
- Heading-led section parents, document-title versus later-page-title roles, and structured contact fields for organization, postal address, phone, email, and website.
- Repeated portrait-card detection and profile hierarchy reconstruction, including photograph, name/role heading, and complete adjacent biography text without false two-column splitting.
- Separate page `completeness_status`: structurally valid JSON no longer implies that high-confidence semantic evidence or review blocks are complete.
- Page-boundary clipping for every normalized block box and review status for repeated artwork that spatially overlaps semantic text.
- Page-organized output exactly at the requested boundary: `source-blocks/document-manifest.json` and `source-blocks/page-NNN.json`.

PaddleOCR table structure is an optional future specialist. The working fallback does not pretend that OCR line proximity is validated table structure: those blocks stay in review until an adapter or human validates ownership.

The included US-state reference is the [US Census Bureau 2025 cartographic boundary file](https://www.census.gov/geographies/mapping-files/2025/geo/carto-boundary-file.html), packaged for offline runs. It is geographic reference data, not Coulton-specific extraction data. Other countries or map projections require their own registered reference adapter; the pipeline must not invent owners for them.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[ocr,validate,dev]"
```

Poppler's `pdftoppm` must be installed and available on `PATH`, or supplied with `--pdftoppm`.
RapidOCR downloads its standard models when needed. For an existing local model directory, set
`DEALSYNQ_RAPIDOCR_MODELS` or pass `--rapidocr-models`.

For local Qwen vision, use the non-thinking model with the included 16K context profile. The larger context is
important for dense maps containing many OCR evidence IDs:

```powershell
ollama pull qwen3-vl:4b-instruct
ollama create dealsynq-qwen3-vl:4b-instruct-16k -f .\ollama\Qwen3-VL-4B-Instruct.Modelfile
```

## Run

```powershell
.\.venv\Scripts\python -m ocr_pipeline 'C:\path\input.pdf' `
  --output '.\runs\my-immutable-run' `
  --dpi 300
```

Useful options:

- `--pages 1-3,8` processes a subset while retaining the source document's true page count.
- `--dpi 300` increases render resolution for dense small text.
- `--skip-ocr` is a quick native-PDF smoke-test mode. It is not appropriate for scanned or visual pages.
- `--rapidocr-python PATH` and `--rapidocr-models PATH` override automatic discovery.
- `--qwen-endpoint http://localhost:11434/v1/chat/completions --qwen-model MODEL` enables low-confidence classification plus a mandatory Qwen semantic stage for every table, chart, and map. For Ollama, start with `qwen3-vl:4b-instruct`; use the included `dealsynq-qwen3-vl:4b-instruct-16k` profile for dense maps. Avoid the ambiguous `qwen3-vl:4b` tag because it may resolve to the thinking variant. Set `QWEN_API_KEY` only if the endpoint requires it.

Outputs are immutable by design: the target directory must not exist. A failed run retains its manifest and partial evidence with `status: failed`.

## Output contract

```text
run/
|-- manifest.json
|-- inspection/
|   |-- manifest.json
|   |-- page-001.json
|   `-- page-002.json
|-- diagnostics/
|   `-- vision-features/
|       `-- p001-r001.json
|-- source/
|   `-- original-name.pdf
|-- page-images/
|   `-- page-001.png
|-- region-images/
|   `-- p001-r001.png
|-- work/
|   |-- rapidocr-jobs.json
|   |-- rapidocr-results.json
|   `-- rapidocr.log
`-- source-blocks/
    |-- document-manifest.json
    |-- page-001.json
    `-- page-002.json
```

Every block carries `document_id`, `page`, `block_id`, `semantic_role`, explicit hierarchy, type-specific `content`, normalized source coordinates, extraction methods, confidence, validation, and provenance. Structural `group` blocks own related children without owning their OCR evidence. Table cells, chart observations, KPI metrics, and map bindings remain nested under their semantic parent instead of masquerading as independent page blocks. Page files use `unified-source-page/4.0`, blocks use `unified-source-block/3.0`, the collection uses `unified-source-collection/3.0`, page inspection uses `pdf-page-inspection/1.0`, and visual diagnostics use `opencv-region-diagnostic/1.0`. Dedicated schemas are in `schemas/`.

Run artifact references are stored relative to the run root so a complete run remains valid after it is copied or cloned on another machine.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

For a fast end-to-end verification on a digitally generated PDF, add `--pages 1 --skip-ocr`. For production evidence recovery, do not skip OCR because chart/map labels and unreliable hidden text layers require the visible-text channel.

## Validate an output collection

Install the `validate` extra, then independently validate every block against the Draft 2020-12 schema and verify source, page JSON, and rendered-evidence hashes:

```powershell
.\.venv\Scripts\dealsynq-validate-source-blocks '.\runs\my-immutable-run' `
  --output '.\validation-reports\my-run.json'
```

`schema_valid` and `integrity_valid` answer whether the JSON contract and artifacts are sound. They do not turn `needs_review` extraction candidates into approved evidence.

## Complete example run and audit

The latest full 16-page, 300-DPI RapidOCR + OpenCV + Qwen3-VL 0.7.0 run is
[`examples/coulton-creek-v070-full-qwen-audited-r1`](examples/coulton-creek-v070-full-qwen-audited-r1/README.md).
It includes the preserved source, per-page PDF inspection, rendered evidence, region crops,
OCR output, diagnostics, per-page Unified Source Blocks, and an independent validation report.
The [page-by-page audit](reports/COULTON_CREEK_FULL_PIPELINE_AUDIT_2026-09-15.md)
explains what works, what remains misleading, and the general fixes still needed.

Earlier historical outputs remain at
[`examples/coulton-creek-qwen3vl4b-v050-final-r3`](examples/coulton-creek-qwen3vl4b-v050-final-r3/README.md)
and the page-7-only
[`examples/coulton-creek-v060-page007-qwen-final-r3`](examples/coulton-creek-v060-page007-qwen-final-r3/README.md).

## Current-model vision-only benchmark

The [Qwen3-VL image-only versus complete-pipeline benchmark](reports/vision-ablation-qwen3vl/RESULTS_2026-09-15.md)
uses the published 16-page run as its comparator. It includes 65 visibly checked content/ownership
anchors, raw model outputs, a scoring script, a short PDF brief, and explicit reliability/runtime
limits. Vision-only is stronger on some chart and two-column text semantics; the complete pipeline
is stronger on table-cell and map ownership and remains the only evidence-backed Unified Source
Block output. Neither side should be considered content-approved on this one development PDF.

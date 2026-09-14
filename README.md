# DealSynq OCR pipeline through Unified Source Blocks

Current package version: `0.5.0`.

This folder implements the requested architecture boundary:

`PDF -> ingestion -> inspection -> page/region routing -> typed extraction -> reconstruction/validation -> Unified Source Block Collection`

It deliberately performs no semantic entity, fact, relation, embedding, retrieval, or DeepSeek work.

## What is implemented

- Immutable run directories, stable document IDs, SHA-256 source identity, and a byte-identical preserved source copy.
- Deterministic PDF inspection with native character quality, positioned text, image coverage, vector counts, table candidates, page dimensions, and rotation.
- Region-level routing for mixed pages. Low-confidence image regions can be sent to a Qwen3-VL endpoint for classification.
- Native-text usability routing. A text layer is used only when it passes the configured quality threshold; otherwise RapidOCR supplies visible text and coordinates.
- Native table parsing into one hierarchical table block with owned columns, rows, typed cells, cell coordinates, and cell evidence IDs. When native structure is unavailable, the result stays `needs_review`.
- Logical visual-object merging before classification, including fragmented PDF chart images and repeated page-band decoration detection.
- PDF-object completeness detection for vector bar clusters and dense raster point/label clusters. Incomplete bar and scatterplot reconstructions are forced to `needs_review` instead of silently passing as text.
- Chart/map OCR, OpenCV geometry, optional Qwen3-VL routing, and nested observations/bindings. Deterministic chart label/value bindings retain evidence IDs and coordinates; model confidence alone cannot mark a visual fact as grounded.
- Generic reconstruction of rotated native-PDF chart values plus x-aligned bar ownership. This recovers vertical percentage labels without page-number or document-value hardcoding.
- Native positioned financial-table reconstruction that infers columns from repeated numeric alignment, recovers headers and sections from geometry, and reconciles generic subtotals against the final total.
- Raw OpenCV arrays are isolated in hashed `diagnostics/vision-features/*.json` artifacts. Source blocks expose only compact `vision_summary`, `vision_features_ref`, and `region_image` fields.
- A common source-block envelope, explicit errors/warnings, provenance, confidence, normalized coordinates, native-versus-visible OCR agreement, page-level validation, and a complete OCR evidence ledger/disposition list.
- Page-organized output exactly at the requested boundary: `source-blocks/document-manifest.json` and `source-blocks/page-NNN.json`.

PaddleOCR table structure is an optional future specialist. The working fallback does not pretend that OCR line proximity is validated table structure: those blocks stay in review until an adapter or human validates ownership.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[ocr,validate,dev]"
```

Poppler's `pdftoppm` must be installed and available on `PATH`, or supplied with `--pdftoppm`.
RapidOCR downloads its standard models when needed. For an existing local model directory, set
`DEALSYNQ_RAPIDOCR_MODELS` or pass `--rapidocr-models`.

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
- `--qwen-endpoint http://localhost:11434/v1/chat/completions --qwen-model MODEL` enables low-confidence classification and visual grounding. Set `QWEN_API_KEY` only if the endpoint requires it.

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

Every root block carries `document_id`, `page`, `block_id`, type-specific `content`, normalized source coordinates, extraction methods, confidence, validation, and provenance. Table cells, chart observations, KPI metrics, and map bindings are nested under their semantic parent instead of masquerading as independent page blocks. Page files use `unified-source-page/3.0`, the collection uses `unified-source-collection/2.0`, page inspection uses `pdf-page-inspection/1.0`, and visual diagnostics use `opencv-region-diagnostic/1.0`. Dedicated schemas are in `schemas/`.

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

## Complete example run

A full 16-page, 300-DPI RapidOCR + OpenCV + Qwen3-VL run is published in
[`examples/coulton-creek-qwen3vl4b-v050-final-r3`](examples/coulton-creek-qwen3vl4b-v050-final-r3/README.md).
It includes the preserved source, rendered evidence, region crops, OCR output, diagnostics,
Unified Source Blocks, and an independent validation report.

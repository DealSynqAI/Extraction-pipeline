# Coulton Creek full pipeline example

This directory is a complete 16-page pipeline run produced with:

- DealSynq extraction pipeline 0.4.1
- 300-DPI Poppler rendering
- RapidOCR 3.9.2 / PP-OCRv6
- OpenCV region diagnostics
- Local `qwen3-vl:4b` classification for low-confidence visual regions

The run contains the preserved source PDF, page images, region crops, raw OCR evidence,
hashed vision diagnostics, page-organized Unified Source Blocks, and a validation report.

Validation summary:

- 16 pages
- 220 Unified Source Blocks
- 205 passed
- 15 marked `needs_review`
- 0 JSON Schema errors
- 0 integrity errors
- Result: valid

Important files:

- [`manifest.json`](manifest.json) - run configuration, stages, and tool versions
- [`source-blocks/document-manifest.json`](source-blocks/document-manifest.json) - collection index
- [`validation-report.json`](validation-report.json) - independent schema and integrity validation
- [`source/`](source/) - preserved input PDF

Model classifications can vary between runs. `needs_review` is an intentional evidence-quality
state and must not be interpreted as schema failure or content approval.

# Coulton Creek full validation run — pipeline 0.5.0

This immutable run processed all 16 pages at 300 DPI with RapidOCR PP-OCRv6,
OpenCV/PDF geometry, and `qwen3-vl:4b` for low-confidence visual routing.

- Source SHA-256: `cf569a8b25fcd36301b34e0e976e88227faa9f9cdc7804f34eddad09e8d477ad`
- Page files: `source-blocks/page-NNN.json`
- Per-page inspection: `inspection/page-NNN.json`
- Inspection index: `inspection/manifest.json`
- Independent validation: `validation-report.json`
- Human visual audit: `audit/page-NNN.md`

The validator reports 77 root semantic blocks: 63 passed and 14 need review.
That is a contract/grounding result, not a measured accuracy percentage. Pages
3, 5, 8, 9, 10, and 12 still contain known extraction limitations documented
in their audit files and must not be treated as fully approved financial facts.


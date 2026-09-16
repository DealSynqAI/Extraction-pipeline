# Coulton Creek Page 7 validation run - pipeline 0.6.0

This immutable, page-scoped run processes page 7 of the 16-page source PDF at
200 DPI with RapidOCR PP-OCRv6, PDF/OpenCV geometry, and Qwen3-VL semantic
linking. It is not a rerun or approval of the other 15 pages.

- Source SHA-256: `cf569a8b25fcd36301b34e0e976e88227faa9f9cdc7804f34eddad09e8d477ad`
- Final page blocks: `source-blocks/page-007.json`
- PDF inspection: `inspection/page-007.json`
- Independent collection validation: `validation-report.json`
- Result: 7/7 blocks passed, no unassigned OCR evidence, page complete
- Pie chart: 10/10 visible observations, 100% total, 10 distinct PDF soft-mask references

The `raw_value` and `numeric_value` fields preserve the visible chart values.
`opacity_weighted_area_share_percent` is a geometric evidence measurement, not
a replacement financial value. It may differ slightly from the printed,
rounded percentage, particularly for very small slices.

The runtime rules are general: they use text equivalence, spatial containment,
heading position, one-to-one evidence ownership, and percentage-concordant PDF
soft masks. This PDF's labels and values are regression evidence, not special
conditions in the implementation.

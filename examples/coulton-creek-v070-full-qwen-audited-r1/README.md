# Complete 0.7.0 Coulton Creek run

This is the **completed 16-page run** used in the [15 September 2026 pipeline audit](../../reports/COULTON_CREEK_FULL_PIPELINE_AUDIT_2026-09-15.md). It is a copy of the immutable local run completed at 2026-09-16 01:24 UTC; it was not regenerated or selectively edited for publication.

The package includes `manifest.json`, the preserved source PDF, 16 `inspection/page-NNN.json` files, 16 rendered `page-images/page-NNN.png` files, 46 region crops, OCR jobs/results/log, vision diagnostics, 16 `source-blocks/page-NNN.json` files, `source-blocks/document-manifest.json`, and an independently regenerated `validation-report.json` for this copied folder.

The manifest records RapidOCR/PP-OCRv6, OpenCV and Qwen3-VL in the intended chart/map sequence. Page 6 used the usable-native-table branch, with Qwen review and Python reconstruction; the **non-native PaddleOCR branch remains unimplemented and untested**. Validation passes structurally (16 pages, 149 blocks, zero schema/integrity errors), but 12 blocks need review, and the [audit](../../reports/COULTON_CREEK_FULL_PIPELINE_AUDIT_2026-09-15.md) describes further semantic mistakes that status counts miss. Do not mistake this package for content-approved financial or legal data.

Run `python -m ocr_pipeline.validate_run ".\examples\coulton-creek-v070-full-qwen-audited-r1"` after cloning to verify the relative artifact references and hashes on your own machine.

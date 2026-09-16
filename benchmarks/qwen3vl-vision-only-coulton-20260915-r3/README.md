# Current-model image-only benchmark: primary 16-page pass

This is the primary Qwen3-VL **image-only** pass for the [current-model ablation report](../../reports/vision-ablation-qwen3vl/RESULTS_2026-09-15.md). Each of pages 1-16 received only its rendered PNG from the [complete example run](../../examples/coulton-creek-v070-full-qwen-audited-r1/README.md), generic prompt/schema and page-number metadata. No OCR text, PDF positions, OpenCV diagnostics, map reference or full-run extraction was provided to the model.

`page-NNN.json` is parseable model output; `page-NNN.raw.txt` is the returned model text; `page-NNN.receipt.json` records time and completion. Page 5 has **no parsed JSON** because both the primary call and the same-prompt retry produced an unterminated repetitive description. The primary failure receipt is `page-005.receipt.json`; `retry-page005/` preserves the second call's raw response and receipt. `score.json` contains the 65 development-reference anchor results and separate full-output agreement diagnostics. The reference and scoring code are in `reports/vision-ablation-qwen3vl/`.

This is a lightweight semantic JSON contract, **not** a generated Unified Source Block collection. The [report](../../reports/vision-ablation-qwen3vl/RESULTS_2026-09-15.md) explains why parseable JSON and selected-anchor matches cannot replace evidence-backed content approval.

# Exploratory compact-prompt run: aborted, not scored

This is a **partial** prompt-sensitivity attempt, not a second complete benchmark. The same Qwen3-VL model and image-only input were used, but a general compact/anti-repetition/map-uniqueness instruction was added. Pages 1 and 3 returned JSON; pages 2 and 4 returned unterminated repeat-loop JSON. The run was stopped before page 5, so no aggregate quality or 16-page latency should be inferred from this folder. Raw failure text and receipts are retained to make that limitation inspectable.

The [report](../../reports/vision-ablation-qwen3vl/RESULTS_2026-09-15.md) and primary [r3 run](../qwen3vl-vision-only-coulton-20260915-r3/README.md) are the decision evidence. The runner reproduces this exploratory prompt with `--prompt-profile compact`.

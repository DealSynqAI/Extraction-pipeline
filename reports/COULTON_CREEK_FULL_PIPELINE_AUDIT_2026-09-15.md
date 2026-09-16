# Coulton Creek full-pipeline audit — 15 September 2026

## Bottom line

The latest **complete** run was made immediately before this audit, so it was not repeated unnecessarily. It processed all 16 pages of the Coulton Creek financial overview at 300 DPI through PDF ingestion, **per-page** inspection, page/region routing, extraction, and Unified Source Block collection. RapidOCR/PP-OCRv6, OpenCV, and Qwen3-VL were enabled. The finished run is published at [`examples/coulton-creek-v070-full-qwen-audited-r1`](../examples/coulton-creek-v070-full-qwen-audited-r1/README.md).

The output is **technically valid, but not fully content-approved**. Independent validation found 149 schema-valid, integrity-valid blocks: 137 `passed`, 12 `needs_review`; 10 pages are structurally complete and 6 require review. A rendered-page comparison found additional semantic mistakes that the current validator does not catch, especially the two-column legal text on page 16. The collection should therefore be treated as *evidence with explicit review flags*, not a final financial-data feed.

## What actually ran

- Source: `GP - Coulton Creek Capital Q42023 Company Financial Overview.pdf`; SHA-256 `cf569a8b25fcd36301b34e0e976e88227faa9f9cdc7804f34eddad09e8d477ad`.
- Version: `dealsynq-ocr-pipeline/0.7.0`; run began 15 September 2026 at 21:20 EDT (16 September 01:20 UTC) and completed at 21:24 EDT. The saved [`manifest.json`](../examples/coulton-creek-v070-full-qwen-audited-r1/manifest.json) records all seven stages as `complete` and pages 1–16 as selected.
- Inspection is **one JSON file per page**, not one combined inspection result: [`inspection/page-001.json`](../examples/coulton-creek-v070-full-qwen-audited-r1/inspection/page-001.json) through `page-016.json`. The collection likewise has one `source-blocks/page-NNN.json` per page and a document manifest.
- Classification confidence affects **initial routing**; it does **not** skip the later Qwen semantic stage for structured visual regions.

| Region encountered in this PDF | Recorded sequence | Result / caveat |
| --- | --- | --- |
| Usable native text | Inspect text quality → positioned native text → normalization, hierarchy, evidence | Mixed pages retain normal text as well as image/chart/map regions. |
| Page 6 native table | Native PDF table parser → Qwen3-VL structure review → Python cell/row reconstruction and reconciliation | Passed. The architecture's native-table `Yes` branch was taken. |
| Charts, including pages 7 and 9 | RapidOCR → PP-OCRv6 → OpenCV (plus PDF vectors when available) → Qwen3-VL label/mark linking → Python reconstruction | All stages are recorded; page 9 still fails semantic reconciliation. |
| Page 8 map | RapidOCR → PP-OCRv6 → OpenCV and geographic registration → Qwen3-VL binding review → Python reconstruction | All stages are recorded; model conflicts are retained as warnings. |
| Page 10 comparison panel | Positioned PDF words → Python lane/heading/bullet candidates → Qwen3-VL layout review → validated panel | Passed, but a contradictory model-level flag remains visible. |

**Important table limitation:** no non-native table in this PDF exercised the architecture's `No`/PaddleOCR branch. The code currently marks such uncertain table ownership for review; a PaddleOCR structure adapter is not implemented. This run demonstrates the native-table branch and the full chart/map sequence, **not** that every possible table path works. Re-running the same PDF would not test PaddleOCR; that needs a separate scanned/non-native table fixture and implementation.

## Verification performed

The copied, publishable run contains the preserved PDF, 16 rendered page PNGs, 16 inspection page JSONs, 16 Unified Source Block page JSONs, 46 region crops, OCR jobs/results/log, and vision diagnostics (131 files, approximately 21.6 MB). Re-running the independent validator **against the copied folder** passed: 16 pages, 149 blocks, zero JSON-schema errors, zero artifact/evidence-integrity errors. The regression suite also passed **67/67 tests**. Rendered pages 1–16 were compared with their extracted page files; the observations below are content-review findings, not schema-validation results.

The [`validation-report.json`](../examples/coulton-creek-v070-full-qwen-audited-r1/validation-report.json) intentionally says `content_approval_claimed: false`. A `passed` flag means the implemented checks succeeded; it is not a guarantee that the reading order, chart meaning, or every label is correct.

## Page-by-page review and general-rule changes

“Complete” below is the pipeline's structural page status. It does not mean a human has approved every sentence or numeric claim.

| Page | What is on the page and what the current run produces | Assessment and next action |
| --- | --- | --- |
| **1** | Cover title, branding, company-overview text, and legal notice are represented as separate blocks, rather than a giant decorative label. The rules now clip block boxes to the page and flag artwork overlapping semantic text. | **Needs review (3 blocks).** A background/decorative region still overlaps cover text, and parent groups inherit the warning. Improve general background-mask/foreground ownership so one pixel/evidence area is not claimed twice; do not drop the legal notice. |
| **2** | Building photograph, heading, body text, branding, and contact details are separately represented. Region routing does not discard ordinary text just because a photograph is present. | **Structurally complete.** General mixed-page routing and text/photograph separation are working here; spot-check source words before downstream use. |
| **3** | “About Us” narrative and right-side KPI infographic are separated. General infographic recognition creates a chart/metric parent. | **Needs review (2 blocks).** The visible `$277 MILLION`/`168 Investments` and `$146 MILLION`/`61 Investments` clusters are emitted with truncated or wrong categories and missing currency/magnitude units. Add layout-based *KPI cluster* grouping: join an amount, count, caption and date by proximity; reject vague/truncated labels and require evidence for each unit. No page number or known value should be hardcoded. |
| **4** | Native paragraphs and multiple heading-led sections retain their own hierarchy and evidence. | **Structurally complete.** The heading/section rule is general and helps avoid flattening the page into one block. |
| **5** | Three portrait-card regions are represented as photograph + name/role + adjacent complete biography, rather than false two-column prose. | **Structurally complete.** General repeated-card and neighboring-text ownership rules are working on this page. |
| **6** | “Current Portfolio” is one owned table block: 12 rows, 8 columns, typed cells, coordinates and cell evidence. Native structure was usable; Qwen reviewed it; Python reconstructed and reconciled the totals. | **Structurally complete.** Five reconciliation checks pass, including invested subtotals `$77,819,827 + $67,822,760 = $145,642,587` and percentage subtotals `53.4 + 46.6 = 100`. This does not test PaddleOCR on scanned tables. |
| **7** | “Portfolio Allocation” pie is a chart block with 10 observations, `100%` total and `verified` wedge ownership tied to distinct PDF mask evidence. OCR, OpenCV, Qwen and Python were all invoked in order. | **Structurally complete.** The general fix is one observation per label/value/wedge association and geometry-backed slice ownership; an unverified extra Qwen proposal was omitted rather than invented. |
| **8** | “Current Portfolio Investments By State” map produces 27/27 bindings. The general map rule registers a reusable US Census state boundary, then checks interior labels and external leader lines against geometry. Rendered silhouette IoU is `0.97217`. | **Structurally complete, with a caution.** Qwen disagreed with deterministic ownership for 16 values; the model was not allowed to overwrite geometry without corroboration. Registration applies only to compatible US-state maps; other geographies/projections need adapters. Human spot-check of state/value assignments remains advisable. |
| **9** | Two related pie graphics appear within `$146 Million Portfolio`: the second inset subdivides a category in the first. | **Needs review (2 blocks).** The output emits 16 observations for 5 expected and reports `167.7%`, with unresolved slice geometry. It separates dollar values and percentages and creates truncated labels. Add general multi-pie object segmentation and parent/inset relationships; pair amount and percent under one category; track each pie's denominator and do not force the inset to sum independently to 100%. |
| **10** | Three offer lanes are preserved as a comparison panel with 17 owned claims (6 + 6 + 5), headings, bullet anchors, coordinates, numeric mentions and evidence IDs. This fixes the earlier false one-row “table” reading using general positioned-word/lane rules. Qwen reviewed the layout. | **Structurally complete.** Qwen's overall `structure_matches` flag says false although its explicit layout fields agree, so the contradictory flag is kept as a warning; it should not silently overrule word/geometry evidence. |
| **11** | Annual IRR vector/bar chart has 10/10 observations; OCR, OpenCV, PDF vectors, Qwen and Python are recorded. | **Structurally complete, with a caution.** Qwen disagreed with three deterministic bindings and grounded only three of ten; bar/value ownership passed deterministic checks but merits sampling before financial reuse. |
| **12** | Dense “Realized Investments by Month - IRR” scatterplot: the page states 105 investments and shows date/IRR axes, colored dots and explanatory bullets. | **Needs review (1 chart block; additional text concern).** The chart expects 105 points but emits only one, incorrectly treating an axis `100%` tick as an “Average” observation. The body text block also merges heading and bullets despite passing. Add plot-boundary and axis calibration, dot-center detection/series binding and explicit “not recoverable” point values; exclude axis ticks from observations and split heading from bullets. |
| **13** | Senior-unit coverage bar chart is reconstructed from OCR, OpenCV, PDF vectors, Qwen and Python. | **Needs review (2 blocks).** The orphan word `floor.` is emitted separately from the sentence ending in a 6.5% floor, while the chart itself passes. Add baseline/font/proximity-based continuation joining and validator detection of short trailing-word orphans. |
| **14** | Quarterly Class B yield vector/bar chart has 23/23 observations and passes implemented geometry checks. | **Structurally complete, with a caution.** Qwen proposed 23 ownership links that Python could not independently corroborate; they were omitted. Spot-check labels and dates rather than treating model suggestions as evidence. |
| **15** | Contact page has structured contact blocks and brand marks, with readable fields instead of merged text. | **Structurally complete.** General contact-field parsing and page hierarchy are working here. |
| **16** | Two-column legal disclosures are extracted into many text fragments. | **Needs review (2 flagged blocks), but the flagged count understates the problem.** Several `passed` paragraphs interleave left- and right-column lines into misleading prose; an isolated `Company.` is also detached. This is the highest-priority validator blind spot. Detect column x-lanes/gutters from positioned words, read each column vertically, join paragraphs within a lane, and fail/review any cross-column leakage even if every OCR word is assigned. Do not publish legal wording as approved until checked against the page. |

## What changed, and what is still general

The recently developed rules are **geometry/evidence-based**, not Coulton-specific string or page exceptions: mixed text/image routing; separate logo/background/body ownership; heading-led section hierarchy; repeated portrait cards; financial-table cell ownership and subtotal checks; pie observation/wedge evidence; US-state registration against a packaged Census geographic reference; PDF-vector bar association; and positioned-word comparison lanes. The map reference is domain-specific to *US-state choropleths*, not this company's map. Pages that do not fit a registered domain reference remain in review rather than receiving guessed labels.

The latest work also prevents a comparison card on page 10 from masquerading as a one-row financial table, and adds checks for unassigned evidence and incomplete visual observations. These changes improved the run's reported page status, but they did **not** solve the page 3, 9, 12, 13 and 16 content issues above. Generalization needs tests on PDFs with different layouts; the one-document audit alone cannot prove it.

## Remaining work, in priority order

1. **Correct reading order and strengthen validation for multi-column text (page 16).** Test with two-column legal pages and ensure no text block crosses lanes; compare assembled text visually. This is a content-integrity problem even though many affected blocks currently say `passed`.
2. **Repair hierarchical chart semantics (pages 9 and 3).** Segment independent/inset pies and KPI clusters; bind each label, value, unit, geometry and denominator to one owner; fail validation on split dollar/percentage observations or vague labels.
3. **Handle dense scatterplots honestly (page 12).** Reconstruct plotted marks only when position/axis calibration is adequate. If precise point values cannot be read, emit positions/uncertainty and `needs_review`, never an axis tick as data.
4. **Join orphan text and improve background ownership (pages 13 and 1).** Use reusable line-continuation and foreground/background evidence rules; add tests with different designs.
5. **Implement and benchmark the non-native/PaddleOCR table branch.** Use a scanned-table fixture, then run OCR structure → Python reconstruction → cell/evidence validation; retain `needs_review` when ownership is weak. This PDF's page 6 cannot prove that branch.
6. **Calibrate model-conflict status (pages 8, 10, 11, 14).** Deterministic geometry should continue to win when the model conflicts, but model disagreement should trigger targeted review thresholds rather than being either ignored or allowed to invent ownership.
7. **Run a cross-document regression and manual content audit.** The 67 tests and one full PDF prove executable behavior, not universal accuracy. Use independent PDFs with scanned tables, multiple pies, complex maps and multi-column text; measure observation recall, label/value accuracy, reading order and review-flag precision.

## Reproduce or validate

The published run is immutable evidence. To validate it after cloning:

```powershell
python -m pip install -e ".[ocr,validate,dev]"
python -m pytest -q
python -m ocr_pipeline.validate_run ".\examples\coulton-creek-v070-full-qwen-audited-r1" --output ".\validation-reports\cloned-run-check.json"
```

To make a **new** full run rather than alter the published one, supply Poppler, working RapidOCR and the local Qwen model, then use a fresh output directory:

```powershell
python -m ocr_pipeline ".\examples\coulton-creek-v070-full-qwen-audited-r1\source\GP - Coulton Creek Capital Q42023 Company Financial Overview.pdf" `
  --output ".\runs\new-full-run" --dpi 300 `
  --qwen-endpoint "http://localhost:11434/v1/chat/completions" `
  --qwen-model "dealsynq-qwen3-vl:4b-instruct-16k"
```

An independent validator checks schema and artifact integrity; rendered-page and evidence comparison is still required for content approval.

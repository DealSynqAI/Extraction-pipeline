"""Build the two-page, evidence-limited vision/OCR ablation brief."""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output" / "pdf" / "dealsynq_vision_vs_ocr_ablation.pdf"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

NAVY = colors.HexColor("#13253B")
TEAL = colors.HexColor("#087E87")
PALE = colors.HexColor("#E8F5F4")
INK = colors.HexColor("#223348")
MUTED = colors.HexColor("#536778")
RULE = colors.HexColor("#D8E2E8")
ORANGE = colors.HexColor("#A9551B")

styles = {
    "title": ParagraphStyle(
        "Title", fontName="Helvetica-Bold", fontSize=20, leading=23,
        textColor=NAVY, spaceAfter=8,
    ),
    "subtitle": ParagraphStyle(
        "Subtitle", fontName="Helvetica", fontSize=9, leading=13,
        textColor=MUTED, spaceAfter=12,
    ),
    "h1": ParagraphStyle(
        "H1", fontName="Helvetica-Bold", fontSize=12, leading=16,
        textColor=NAVY, spaceBefore=9, spaceAfter=5,
    ),
    "body": ParagraphStyle(
        "Body", fontName="Helvetica", fontSize=8.8, leading=13,
        textColor=INK, spaceAfter=6,
    ),
    "small": ParagraphStyle(
        "Small", fontName="Helvetica", fontSize=7.5, leading=10.3,
        textColor=MUTED, spaceAfter=4,
    ),
    "tiny": ParagraphStyle(
        "Tiny", fontName="Helvetica", fontSize=6.75, leading=9.2,
        textColor=MUTED,
    ),
    "tablehead": ParagraphStyle(
        "TableHead", fontName="Helvetica-Bold", fontSize=7.5, leading=10,
        textColor=colors.white,
    ),
    "table": ParagraphStyle(
        "Table", fontName="Helvetica", fontSize=7.5, leading=10.4,
        textColor=INK,
    ),
    "callout": ParagraphStyle(
        "Callout", fontName="Helvetica-Bold", fontSize=9, leading=13,
        textColor=NAVY,
    ),
}


def p(text, style="body"):
    return Paragraph(text, styles[style])


def callout(text):
    table = Table([[p(text, "callout")]], colWidths=[177 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE),
        ("BOX", (0, 0), (-1, -1), 0.4, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def grid(headers, rows, widths):
    data = [[p(h, "tablehead") for h in headers]]
    data += [[p(str(cell), "table") for cell in row] for row in rows]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FA")]),
        ("GRID", (0, 0), (-1, -1), 0.35, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def page_chrome(canvas, doc):
    canvas.saveState()
    width, height = A4
    canvas.setFillColor(NAVY)
    canvas.rect(0, height - 8 * mm, width, 8 * mm, fill=1, stroke=0)
    canvas.setStrokeColor(RULE)
    canvas.line(16 * mm, 18 * mm, width - 16 * mm, 18 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(16 * mm, 13 * mm, "DealSynq  |  evidence brief  |  15 Sep 2026")
    canvas.drawRightString(width - 16 * mm, 13 * mm, f"{doc.page} / 2")
    canvas.restoreState()


doc = BaseDocTemplate(
    str(OUTPUT), pagesize=A4,
    leftMargin=16 * mm, rightMargin=16 * mm,
    topMargin=17 * mm, bottomMargin=23 * mm,
    title="DealSynq: Qwen-only versus OCR/geometry evidence",
    author="DealSynq extraction engineering",
)
frame = Frame(16 * mm, 23 * mm, 178 * mm, 257 * mm, leftPadding=0,
              rightPadding=0, topPadding=0, bottomPadding=0)
doc.addPageTemplates(PageTemplate(id="brief", frames=[frame], onPage=page_chrome))

story = [
    p("Can Qwen vision replace OCR and Python?", "title"),
    p("A measured answer from prior Coulton Creek runs. These are development benchmarks, not a production accuracy claim.", "subtitle"),
    callout("<font color='#087E87'>Short answer:</font> No, not as an unguarded single extractor in the tested setup. Qwen is valuable for visual meaning; native text/OCR preserve literal evidence, and geometry plus validation preserve structure."),
    p("1. Same-page, same-model ablation: dense financial table (page 6)", "h1"),
    p("Qwen3.5:9B used the same page, prompt, schema, decoding settings and frozen 61-fact reference; only the supplied image/OCR input changed. <b>\"OCR-only\" here means Qwen reading OCR text, not a RapidOCR/Python-only pipeline.</b>", "small"),
    grid(
        ["Qwen input", "Exact complete facts", "Other useful signal", "Wall time"],
        [
            ("Image only", "0 / 61", "95.1% evidence recall; 23 unsupported/duplicate facts", "20.4 min"),
            ("OCR text only", "0 / 61", "100% evidence recall; 16 unsupported/duplicate facts", "22.8 min"),
            ("OCR + image", "7 / 61 (11.5% recall; 17.5% F1)", "3 unsupported/duplicate facts", "9.6 min"),
        ],
        [38 * mm, 45 * mm, 72 * mm, 23 * mm],
    ),
    Spacer(1, 4 * mm),
    p("A fact counted only when <i>all</i> owner, page/section/row, relation, printed value, period, modality, polarity, condition and evidence fields matched. Thus 0/61 does <b>not</b> mean the model read zero characters; it means no entire fact record passed this strict test. The reference was assistant-verified, not independently human-audited.", "small"),
    p("2. Cover-page legal detail (page 1)", "h1"),
    grid(
        ["Method", "Semantic items", "Observed failure"],
        [
            ("Qwen3.5:4B, page image only", "6 / 12", "Inverted the \"no money solicited\" restriction; omitted legal details."),
            ("Qwen3.5:4B, image + native + OCR", "8 / 12", "Valid JSON, but still omitted four reference items."),
            ("Native text / independent OCR", "12 / 12 each", "Literal semantics available, but neither captured the brand graphic role."),
        ],
        [53 * mm, 30 * mm, 95 * mm],
    ),
    Spacer(1, 2 * mm),
    p("The 4B cover test and 9B table test are different models/tasks; their fractions must not be pooled.", "small"),
    p("3. What OCR alone actually measures", "h1"),
    p("Fresh 16-page, 200-DPI RapidOCR 3.9.2 + PP-OCRv6 bake-off: <b>94.68% macro token F1</b>, <b>93.15% numeric F1</b>, <b>89.03% numeric recall</b>, 0 failed pages, <b>2.17 sec/page</b>. It read page-6 table text at about 99% token F1, yet emitted text lines—not a trustworthy row/column grid. On a separate 31-page / 7-PDF stress set: <b>87.59% token F1</b>, <b>85.85% numeric F1</b>, 100% normal completion, <b>3.11 sec/page</b>.", "body"),
    p("OCR transcription F1 and source-block/fact F1 are different endpoints; there is no directly comparable \"Qwen versus RapidOCR accuracy\" number here.", "small"),
    PageBreak(),
    p("Why a single-stage approach breaks", "title"),
    p("Failures observed in the recorded trials, and the engineering implication for the current pipeline.", "subtitle"),
    grid(
        ["Observed failure", "Why it matters", "Guardrail"],
        [
            ("Literal text changes or is compressed", "A negated legal restriction was inverted on page 1; omitted clauses alter meaning.", "Keep native/OCR text and require evidence-grounded checks for negation and conditions."),
            ("Table values lose their cell/section owner", "Page 6 had high evidence coverage but near-zero exact complete facts in direct ablations.", "Use PDF/table geometry; reconstruct cells and section scope deterministically; verify printed tokens."),
            ("JSON can parse without being faithful", "The page-1 hybrid produced valid JSON but only 8/12 required semantic items.", "Validate both schema and source completeness/grounding; do not equate \"valid JSON\" with correct extraction."),
            ("Latency and long outputs", "The 9B page-6 direct calls took 9.6-22.8 minutes on the recorded local runtime.", "Keep VLM tasks narrow and bounded; stop repetition/truncation; reserve models for semantic linking."),
        ],
        [50 * mm, 66 * mm, 62 * mm],
    ),
    Spacer(1, 4 * mm),
    callout("The result is a <font color='#087E87'>division of labor</font>: literal acquisition (native text + RapidOCR), structural recovery (PDF geometry / Python / OpenCV), mandatory Qwen visual-semantic linking for charts and maps, then deterministic reconstruction and source-backed validation."),
    p("What the historical tests do—and do not—prove", "h1"),
    p("<b>They prove:</b> image-only Qwen3.5:4B missed half the cover's semantic items; direct Qwen3.5:9B table-input variants failed strict complete-fact scoring; high OCR transcription does not itself produce a reliable source-block hierarchy.", "body"),
    p("<b>They do not prove:</b> that every Qwen model fails, that RapidOCR/Python alone can solve charts/maps, or that a current Qwen3-VL-only run has been scored on the same 16-page rubric. The recent Page 7 10/10 pie observations came from the <i>combined</i> Qwen3-VL + OCR/native + PDF/OpenCV/Python pipeline, not Qwen alone.", "body"),
    p("A post-review Page 6 OCR structural-rules experiment reached 61/61 on that one page. Because its rules were developed after inspecting Page 6 and the OCR already contained a clean HTML table, this is a development result—not unseen-document generalization.", "small"),
    p("Recommendation / next valid benchmark", "h1"),
    p("Keep all three structured stages. For Rahul's proposed replacement, freeze one blind, multi-document test set and score <b>current Qwen3-VL image-only</b>, <b>native+RapidOCR+geometry without VLM</b>, and <b>full hybrid</b> on the same pages, same source-block reference, exact cell/label bindings, completeness, unsupported facts, schema validity, and runtime. Only that is a head-to-head architecture decision.", "body"),
    p("Evidence and reproducibility", "h1"),
    p("Original PDF SHA-256: cf569a8b25fcd36301b34e0e976e88227faa9f9cdc7804f34eddad09e8d477ad. Local recorded artifacts: <font face='Courier'>document-pipeline/page-benchmarks/coulton-page-001.md</font>; <font face='Courier'>pipeline-runs/one-page-p006-evaluation-alternative-methods-20260904-r2/COMPARISON.md</font> and variant manifests; <font face='Courier'>experiments/ocr-bakeoff-from-scratch-20260908/REPORT.md</font>; <font face='Courier'>experiments/ocr-stratified-bakeoff-from-scratch-20260908/REPORT.md</font>. These historical raw artifacts are outside this GitHub repository.", "tiny"),
]

doc.build(story)
print(OUTPUT)

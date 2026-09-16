"""Build a compact, source-backed PDF brief from the measured score.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)


NAVY = colors.HexColor("#203653")
BLUE = colors.HexColor("#3c638a")
TEAL = colors.HexColor("#21808c")
INK = colors.HexColor("#182b40")
MUTED = colors.HexColor("#596b7a")
LIGHT = colors.HexColor("#eef3f7")
PALE = colors.HexColor("#f7f9fb")
ORANGE = colors.HexColor("#b45e26")


def stylebook() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("brief-title", parent=base["Title"], fontName="Helvetica-Bold",
                                fontSize=21, leading=24, textColor=NAVY, spaceAfter=9, alignment=TA_LEFT),
        "subtitle": ParagraphStyle("brief-subtitle", parent=base["Normal"], fontName="Helvetica",
                                   fontSize=10.2, leading=14, textColor=MUTED, spaceAfter=12),
        "h1": ParagraphStyle("brief-h1", parent=base["Heading1"], fontName="Helvetica-Bold",
                             fontSize=12.2, leading=15, textColor=NAVY, spaceBefore=11, spaceAfter=5),
        "h2": ParagraphStyle("brief-h2", parent=base["Heading2"], fontName="Helvetica-Bold",
                             fontSize=10.1, leading=12.5, textColor=BLUE, spaceBefore=8, spaceAfter=3),
        "body": ParagraphStyle("brief-body", parent=base["BodyText"], fontName="Helvetica",
                               fontSize=9, leading=12.6, textColor=INK, spaceAfter=5),
        "small": ParagraphStyle("brief-small", parent=base["BodyText"], fontName="Helvetica",
                                fontSize=7.7, leading=10.4, textColor=MUTED, spaceAfter=3),
        "table": ParagraphStyle("brief-table", parent=base["Normal"], fontName="Helvetica",
                                fontSize=7.8, leading=10.2, textColor=INK),
        "tablehead": ParagraphStyle("brief-tablehead", parent=base["Normal"], fontName="Helvetica-Bold",
                                    fontSize=7.9, leading=10.5, textColor=colors.white),
        "metric": ParagraphStyle("brief-metric", parent=base["Normal"], fontName="Helvetica-Bold",
                                 fontSize=14, leading=17, alignment=TA_CENTER, textColor=NAVY),
        "metriclabel": ParagraphStyle("brief-metriclabel", parent=base["Normal"], fontName="Helvetica",
                                      fontSize=7.5, leading=9.8, alignment=TA_CENTER, textColor=MUTED),
        "callout": ParagraphStyle("brief-callout", parent=base["BodyText"], fontName="Helvetica-Bold",
                                  fontSize=9.4, leading=13, textColor=NAVY, spaceAfter=0),
    }


def p(text: str, kind: str, styles: dict) -> Paragraph:
    return Paragraph(text, styles[kind])


def footer(canvas, doc) -> None:
    canvas.saveState()
    width, height = letter
    canvas.setFillColor(NAVY)
    canvas.rect(0, height - 0.12 * inch, width, 0.12 * inch, fill=1, stroke=0)
    canvas.setStrokeColor(colors.HexColor("#d7e2eb"))
    canvas.line(doc.leftMargin, 0.52 * inch, width - doc.rightMargin, 0.52 * inch)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(doc.leftMargin, 0.35 * inch, "DealSynq | current-model extraction benchmark | 16 Sep 2026")
    canvas.drawRightString(width - doc.rightMargin, 0.35 * inch, f"{doc.page} / 3")
    canvas.restoreState()


def score(page: int, method: str, data: dict) -> str:
    result = data["scores"][method][str(page)]
    return f"{result['correct']}/{result['total']}"


def build(score_path: Path, output: Path) -> None:
    data = json.loads(score_path.read_text(encoding="utf-8"))
    assert data["totals"]["vision"] == {"correct": 56, "total": 65}
    assert data["totals"]["full"] == {"correct": 50, "total": 65}
    assert data["vision_page_attempts"]["complete"] == 15
    assert data["vision_page_attempts"]["failed"] == 1
    styles = stylebook()
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output), pagesize=letter,
        leftMargin=0.64 * inch, rightMargin=0.64 * inch,
        topMargin=0.56 * inch, bottomMargin=0.66 * inch,
        title="Qwen3-VL vision-only versus DealSynq complete pipeline",
        author="DealSynq development benchmark",
    )
    width = letter[0] - doc.leftMargin - doc.rightMargin
    story = []

    # Page 1: conclusion and measured scores.
    story.append(p("DEALSYNQ  /  EVIDENCE BRIEF", "small", styles))
    story.append(p("Can Qwen3-VL alone replace the extraction pipeline?", "title", styles))
    story.append(p("Same 16-page Coulton PDF, current Qwen3-VL 4B, image-only versus the complete OCR / OpenCV / Qwen source-block run.", "subtitle", styles))

    callout = Table([[p(
        "<font color='#21808c'>Answer:</font> not wholesale. Qwen alone reads several charts and legal columns better than today's code, "
        "but loses map owners, shifts loan-table cells, and can fail to close JSON. Keep evidence-based verification; let "
        "vision propose repairs where it wins.", "callout", styles)]], colWidths=[width])
    callout.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#d8e5ec")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    story += [callout, Spacer(1, 10)]

    metrics = [
        ("56 / 65", "VISION-ONLY ANCHORS"),
        ("50 / 65", "COMPLETE-PIPELINE ANCHORS"),
        ("15 / 16", "VISION JSON COMPLETION"),
        ("10.2 / 4.2", "MINUTES: VISION / FULL"),
    ]
    metric_row = [[Table([[p(number, "metric", styles)], [p(label, "metriclabel", styles)]], colWidths=[width / 4 - 10])
                   for number, label in metrics]]
    cards = Table(metric_row, colWidths=[width / 4] * 4)
    cards.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALE),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#dce5ec")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dce5ec")),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [cards, Spacer(1, 7)]
    story.append(p("Selected content-and-owner checks", "h1", styles))
    scores = [
        (3, "KPI amount / count / caption", "Vision keeps both clusters"),
        (6, "Financial table cells", "Full protects blank-cell columns"),
        (7, "One pie: category / percent", "Both read all 10"),
        (8, "US map: state / value", "Full owns values by geometry"),
        (9, "Main + inset pie pairs", "Vision reads 4; neither owns inset"),
        (10, "Offer-lane numeric claims", "Both read all 6"),
        (12, "Scatter / text safety", "Vision avoids fake 100% point"),
        (16, "Legal clauses / column order", "Vision restores left-then-right"),
    ]
    rows = [[p("PAGE / TASK", "tablehead", styles), p("VISION", "tablehead", styles),
             p("FULL", "tablehead", styles), p("KEY SIGNAL", "tablehead", styles)]]
    for page, task, signal in scores:
        rows.append([p(f"{page} - {task}", "table", styles), p(score(page, "vision", data), "table", styles),
                     p(score(page, "full", data), "table", styles), p(signal, "table", styles)])
    rows.append([p("Selected total", "tablehead", styles), p("56/65", "tablehead", styles),
                 p("50/65", "tablehead", styles), p("One-PDF development score", "tablehead", styles)])
    table = Table(rows, colWidths=[width * 0.38, width * 0.12, width * 0.11, width * 0.39], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("BACKGROUND", (0, -1), (-1, -1), BLUE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, PALE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, BLUE),
    ]))
    story += [table, Spacer(1, 8)]
    story.append(p("The 65 anchors are heterogeneous and intentionally selected from eight hard pages. This is a descriptive development score, not a blinded multi-document accuracy estimate.", "small", styles))
    story.append(PageBreak())

    # Page 2: decisive evidence and runtime.
    story.append(p("What the new model gets right - and wrong", "title", styles))
    story.append(p("The older brief tested Qwen3.5:4B/9B, not this Qwen3-VL 4B instruct profile. The two score endpoints must not be pooled.", "subtitle", styles))
    story.append(p("Strengths worth using", "h1", styles))
    story.append(p("<b>Pages 3, 7 and 9:</b> Vision-only gets all four KPI cluster anchors, all ten simple-pie labels, and four of five amount/percent pairs in two linked pies. The full run is wrong on page 3 labels and splits page 9 into 16 observations totaling 167.7%. Qwen still omits the 0.7% Return of Capital and does not make a real main/inset chart hierarchy.", "body", styles))
    story.append(p("<b>Pages 11, 13 and 14:</b> On printed bar labels, vision-only and full output agree exactly on 10/10, 10/10 and 23/23 pairs respectively. This is output agreement, not independent truth. <b>Page 16:</b> Vision-only reads the legal columns in the correct left-then-right order; current full text interleaves them while many blocks still claim `passed`.", "body", styles))
    story.append(p("Where source ownership breaks", "h1", styles))

    failures = [
        ("PAGE 6 / TABLE", "72/84 data cells agree on value <i>and column</i>; 12 differ. Qwen shifts $28,045,777 from Non-controlling Interest into Leverage and moves $27,598,623 left. Native-PDF cell geometry catches this."),
        ("PAGE 8 / MAP", "Qwen calls the map a chart and emits 50 map rows against 27 full bindings. Only 7/50 agree; 35 repeat New York, generally with 1.3%. Geography registration and leader lines materially constrain owners."),
        ("PAGE 5 / OUTPUT", "First-pass JSON was 15/16. A 155.5-second repeated photo description ended mid-string; the same temperature-zero retry failed. A compact-prompt probe also failed on pages 2 and 4."),
    ]
    failure_rows = [[p("CASE", "tablehead", styles), p("OBSERVED FAILURE", "tablehead", styles)]]
    failure_rows += [[p(label, "table", styles), p(explanation, "table", styles)] for label, explanation in failures]
    failure_table = Table(failure_rows, colWidths=[width * 0.25, width * 0.75])
    failure_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PALE, colors.white]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#dce5ec")),
    ]))
    story += [failure_table, Spacer(1, 10)]

    story.append(p("Speed and contract", "h1", styles))
    story.append(p("Image-only first attempts: <b>609.4 seconds</b> for 16 pages attempted (including failed page 5). Saved full pipeline: <b>254.5 seconds</b> for 16 pages complete, about 2.39x faster in these separate sessions. The extra 154.7-second image-only retry is excluded. The full output validates all 149 blocks structurally, but its semantic `passed` flag does not catch page 16 column corruption.", "body", styles))
    story.append(p("Image-only JSON used a deliberately lighter semantic-page contract. Exact content/owner anchors are compared head-to-head; evidence IDs, source coordinates, hash-backed crops, per-page inspection and unified hierarchy remain additional deliverables of the complete architecture, not fields that Qwen was asked to produce.", "small", styles))
    story.append(PageBreak())

    # Page 3: decision and limitations.
    story.append(p("A practical step-one decision", "title", styles))
    story.append(p("Rahul's model-first challenge exposed both genuine vision gains and unguarded failure modes.", "subtitle", styles))
    story.append(p("Recommended division of labor", "h1", styles))
    recommendations = [
        ("1. Literal evidence", "Keep native PDF text / RapidOCR for printed words, dates, numbers and negation; compare Qwen claims to visible tokens."),
        ("2. Structural ownership", "Keep PDF/native table cells, OpenCV/vector plot geometry and registered map boundaries where blank cells, wedges, axes and states decide the owner."),
        ("3. Vision candidate", "Use current Qwen3-VL routinely for chart, KPI, comparison and column hypotheses. Accept its better page 3/9/16 readings when independent evidence supports them."),
        ("4. Fail closed", "Reject repeated owners, unsupported values, axis ticks as data, malformed JSON and missing chart hierarchy. Mark unresolved items for human review."),
    ]
    for title, detail in recommendations:
        story.append(KeepTogether([p(title, "h2", styles), p(detail, "body", styles)]))

    story.append(HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#dbe5ec"), spaceBefore=10, spaceAfter=7))
    story.append(p("Fix the complete pipeline, too", "h1", styles))
    story.append(p("General rules still needed: page 3 KPI amount/count/caption clustering; page 9 multiple-pie segmentation and per-pie denominators; page 12 scatter axis-vs-data and point-completeness checks; page 16 column-lane reading order with a cross-column validator. Page 6's non-native PaddleOCR branch remains unimplemented and was not exercised in this PDF.", "body", styles))

    story.append(p("What this benchmark cannot decide", "h1", styles))
    story.append(p("This is one development PDF and a visibly checked 65-anchor reference made with knowledge of previous runs, not a blind human gold set. Model-only receives full page images; full extraction uses crops, OCR and geometry. Runtime is not hardware-randomized. The chosen anchor mix cannot be turned into a production F1 score or a statistically general model ranking. A blind multi-document test with scanned tables, complex maps and multi-column legal pages is the required architecture gate.", "body", styles))

    story.append(p("Reproduce and inspect", "h1", styles))
    story.append(p("Repository folders: <b>reports/vision-ablation-qwen3vl/</b> (reference, scoring code, detailed Markdown) and <b>benchmarks/qwen3vl-vision-only-coulton-20260915-r3/</b> (per-page JSON, raw text, receipts, failures and score). Comparator: <b>examples/coulton-creek-v070-full-qwen-audited-r1/</b>. Source PDF SHA-256: cf569a8b25fcd36301b34e0e976e88227faa9f9cdc7804f34eddad09e8d477ad.", "small", styles))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.score, args.output)
    print(args.output)


if __name__ == "__main__":
    main()

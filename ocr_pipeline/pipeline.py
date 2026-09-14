from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
from typing import Any

from PIL import Image
from pypdf import PdfReader

from .inspection import inspect_pdf
from .models import PageInspection, Region, SourceBlock, validate_source_blocks
from .workers import QwenVisionClient, run_rapidocr_worker


NUMBER = re.compile(r"(?:[$€£]\s*)?\(?\d[\d,.]*(?:\s*(?:%|x|million|billion|mm|k))?\)?", re.I)
MAP_TERMS = {"geographic", "geography", "distribution by state", "investments by state", "portfolio by state", "map"}
CHART_TERMS = {
    "chart", "allocation", "composition", "returns", "irr", "multiple", "series", "axis",
    "portfolio", "portfolio mix", "coverage", "yield", "annualized", "investor alignment",
}
US_GEOGRAPHIES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
}
PIPELINE_VERSION = "0.4.0"
PAGE_SCHEMA_VERSION = "unified-source-page/2.0"
COLLECTION_SCHEMA_VERSION = "unified-source-collection/1.1"
INSPECTION_SCHEMA_VERSION = "pdf-inspection/1.0"
VISION_DIAGNOSTIC_SCHEMA_VERSION = "opencv-region-diagnostic/1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _vision_summary(features: dict[str, Any]) -> dict[str, Any]:
    return {
        "horizontal_line_count": int(features.get("horizontal_lines", 0)),
        "vertical_line_count": int(features.get("vertical_lines", 0)),
        "diagonal_line_count": int(features.get("diagonal_lines", 0)),
        "rectangle_candidate_count": int(features.get("rectangle_candidates", 0)),
        "bar_candidate_count": int(features.get("bar_candidates", 0)),
        "point_candidate_count": int(features.get("point_candidates", 0)),
        "legend_swatch_candidate_count": int(features.get("legend_swatch_candidates", 0)),
        "horizontal_axis_candidate_count": int(features.get("horizontal_axis_candidates", 0)),
        "vertical_axis_candidate_count": int(features.get("vertical_axis_candidates", 0)),
        "plot_boundary_detected": bool(features.get("plot_boundary_detected", False)),
        "table_grid_confidence": round(float(features.get("table_grid_confidence", 0.0)), 5),
        "saturated_pixel_fraction": round(float(features.get("saturated_pixel_fraction", 0.0)), 5),
    }


def _write_vision_diagnostic(
    diagnostics: Path, document_id: str, source_hash: str, region: Region,
    features: dict[str, Any],
) -> dict[str, str]:
    path = diagnostics / f"{region.region_id}.json"
    payload = {
        "schema_version": VISION_DIAGNOSTIC_SCHEMA_VERSION,
        "document_id": document_id,
        "source_sha256": source_hash,
        "page": region.page,
        "region_id": region.region_id,
        "region_coordinates": region.coordinates,
        "coordinate_formats": {
            "region_coordinates": "page-relative [x, y, width, height], normalized 0..1000",
            "feature_boxes": "region-relative [x, y, width, height], normalized 0..1000",
            "line_segments": "region-relative [x1, y1, x2, y2], normalized 0..1000",
        },
        "vision_summary": _vision_summary(features),
        "features": features,
    }
    _write_json(path, payload)
    return {
        "path": str(path.relative_to(diagnostics.parents[1])).replace("\\", "/"),
        "sha256": _sha256(path),
    }


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value or "document"


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    if not spec:
        return list(range(1, page_count + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"Invalid descending page range: {part}")
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    invalid = sorted(page for page in pages if page < 1 or page > page_count)
    if invalid:
        raise ValueError(f"Pages outside 1..{page_count}: {invalid}")
    return sorted(pages)


def clean_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=[a-z])", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs).strip()


def _contains(coordinates: list[float], line: dict[str, Any]) -> bool:
    x, y, w, h = coordinates
    lx, ly, lw, lh = line["coordinates"]
    cx, cy = lx + lw / 2, ly + lh / 2
    return x <= cx <= x + w and y <= cy <= y + h


def _crop(image_path: Path, coordinates: list[float], output_path: Path) -> None:
    with Image.open(image_path) as image:
        width, height = image.size
        x, y, w, h = coordinates
        box = (
            max(0, round(x * width / 1000)), max(0, round(y * height / 1000)),
            min(width, round((x + w) * width / 1000)), min(height, round((y + h) * height / 1000)),
        )
        image.crop(box).save(output_path)


def _render_pages(pdf: Path, pages: list[int], output: Path, dpi: int, pdftoppm: Path | None) -> dict[int, Path]:
    executable = pdftoppm or (Path(shutil.which("pdftoppm")) if shutil.which("pdftoppm") else None)
    if executable is None or not executable.exists():
        raise RuntimeError("pdftoppm was not found; pass --pdftoppm with a Poppler executable")
    rendered = {}
    for page in pages:
        target = output / f"page-{page:03d}"
        subprocess.run([
            str(executable), "-f", str(page), "-l", str(page), "-singlefile",
            "-png", "-r", str(dpi), str(pdf), str(target),
        ], check=True, capture_output=True)
        rendered[page] = target.with_suffix(".png")
    return rendered


def _ocr_text(lines: list[dict[str, Any]]) -> str:
    ordered = sorted(lines, key=lambda line: (line["coordinates"][1], line["coordinates"][0]))
    return "\n".join(str(line["text"]) for line in ordered)


def _token_agreement(left: str, right: str) -> float | None:
    left_tokens = set(re.findall(r"\w+", left.casefold()))
    right_tokens = set(re.findall(r"\w+", right.casefold()))
    if not left_tokens or not right_tokens:
        return None
    return round(len(left_tokens & right_tokens) / len(left_tokens | right_tokens), 5)


def _classify_visual(region: Region, lines: list[dict[str, Any]], features: dict[str, Any]) -> tuple[str, float, list[str]]:
    text = " ".join(str(line["text"]) for line in lines).lower()
    warnings: list[str] = []
    visual_hint = str(region.metadata.get("visual_hint") or "").strip().lower()
    if visual_hint == "chart":
        return "chart", max(0.80, float(region.confidence)), warnings
    strong_map_phrase = any(term in text for term in {"distribution by state", "investments by state", "portfolio by state"})
    map_hits = sum(term in text for term in MAP_TERMS) + sum(name in text for name in US_GEOGRAPHIES)
    chart_hits = sum(term in text for term in CHART_TERMS)
    horizontal = int(features.get("horizontal_lines", 0))
    vertical = int(features.get("vertical_lines", 0))
    rectangles = int(features.get("rectangle_candidates", 0))
    numeric = sum(bool(NUMBER.search(str(line["text"]))) for line in lines)
    if strong_map_phrase or map_hits >= 2:
        return "map", min(0.94, 0.67 + map_hits * 0.06), warnings
    if chart_hits >= 1 and numeric >= 2:
        return "chart", min(0.93, 0.68 + chart_hits * 0.05), warnings
    if horizontal >= 4 and vertical >= 3 and numeric >= 2:
        return "table", 0.76, warnings
    if (horizontal + vertical + rectangles) >= 8 and numeric >= 2:
        return "chart", 0.66, ["visual classification is geometry-only"]
    if len(lines) >= 3 and numeric == 0:
        return "normal_text", 0.62, ["image region treated as scanned text"]
    warnings.append("visual type could not be classified confidently")
    return "unclassified_visual", 0.35, warnings


def _block_type_for_text(region: Region) -> str:
    text = clean_text(region.native_text)
    font = float(region.metadata.get("median_font_size", 0) or 0)
    word_count = int(region.metadata.get("word_count", len(text.split())) or 0)
    y = region.coordinates[1]
    if y >= 890 or (font and font <= 8 and y >= 820):
        return "footnote"
    if word_count <= 14 and (font >= 14 or text.isupper() or text.istitle()):
        return "heading"
    return "text"


def _provenance(source_hash: str, region: Region, ocr_lines: list[dict[str, Any]], image: Path) -> dict[str, Any]:
    return {
        "source_sha256": source_hash,
        "region_id": region.region_id,
        "classification_method": region.classification_method,
        "reading_order": region.reading_order,
        "source_bbox_points": region.metadata.get("source_bbox_points"),
        "ocr_evidence_ids": [line["evidence_id"] for line in ocr_lines],
        "rendered_page": str(image),
    }


def _text_block(
    document_id: str, source_hash: str, inspection: PageInspection, region: Region,
    lines: list[dict[str, Any]], image: Path, native_threshold: float,
) -> SourceBlock:
    visible_ocr = _ocr_text(lines)
    agreement = _token_agreement(region.native_text, visible_ocr)
    use_native = bool(region.native_text.strip()) and inspection.native_text_quality >= native_threshold
    warnings: list[str] = []
    if use_native and agreement is not None and agreement < 0.35:
        use_native = False
        warnings.append("native text disagrees with visible OCR; OCR selected")
    raw = region.native_text if use_native else visible_ocr
    normalized = clean_text(raw)
    methods = ["native PDF text", "Python layout normalization"] if use_native else ["RapidOCR", "PP-OCRv6", "Python layout normalization"]
    errors = [] if normalized else ["no text recovered from region"]
    confidence = inspection.native_text_quality if use_native else (
        sum(float(line["confidence"]) for line in lines) / len(lines) if lines else 0.0
    )
    return SourceBlock(
        document_id=document_id, type=_block_type_for_text(region), page=region.page,
        block_id=f"{region.region_id}-block", parent_block_id=None,
        content={
            "text": normalized, "raw_text": raw,
            "native_text": region.native_text or None,
            "visible_ocr_text": visible_ocr or None,
            "native_ocr_token_agreement": agreement,
        }, coordinates=region.coordinates,
        extraction_method=methods, confidence=confidence,
        validation_status="passed" if normalized else "needs_review", errors=errors, warnings=warnings,
        provenance=_provenance(source_hash, region, lines, image),
    )


def _table_blocks(
    document_id: str, source_hash: str, region: Region, lines: list[dict[str, Any]], image: Path,
) -> list[SourceBlock]:
    rows = region.metadata.get("rows") or []
    table_title = region.metadata.get("title")
    row_sections = region.metadata.get("row_sections") or []
    native = bool(rows and max((len(row) for row in rows), default=0) >= 2)
    if not native:
        ordered = sorted(lines, key=lambda line: (line["coordinates"][1], line["coordinates"][0]))
        grouped: list[list[dict[str, Any]]] = []
        for line in ordered:
            if not grouped or abs(line["coordinates"][1] - grouped[-1][0]["coordinates"][1]) > 16:
                grouped.append([line])
            else:
                grouped[-1].append(line)
        rows = [[item["text"] for item in sorted(group, key=lambda item: item["coordinates"][0])] for group in grouped]
    width = max((len(row) for row in rows), default=0)
    rows = [list(row) + [None] * (width - len(row)) for row in rows]
    reconciliation = _table_total_reconciliation(rows)
    reconciliation_failed = bool(reconciliation and not reconciliation["passed"])
    parent_id = f"{region.region_id}-table"
    warnings = [] if native else ["table reconstructed from OCR geometry; PaddleOCR table structure was unavailable"]
    valid_shape = len(rows) >= 2 and width >= 2
    methods = ["native PDF table parser", "Python table reconstruction"] if native else ["RapidOCR", "PP-OCRv6", "Python table reconstruction"]
    parent = SourceBlock(
        document_id=document_id, type="table", page=region.page, block_id=parent_id, parent_block_id=None,
        content={
            "title": table_title, "rows": rows, "row_count": len(rows), "column_count": width,
            "row_sections": row_sections or None, "reconciliation": reconciliation,
        },
        coordinates=region.coordinates, extraction_method=methods, confidence=region.confidence if native else 0.55,
        validation_status="passed" if valid_shape and native and not reconciliation_failed else "needs_review",
        errors=([] if valid_shape else ["table does not contain at least two rows and two columns"])
        + (["table subtotals do not reconcile with the final total"] if reconciliation_failed else []), warnings=warnings,
        provenance=_provenance(source_hash, region, lines, image),
    )
    blocks = [parent]
    if not rows:
        return blocks
    headers = ["" if value is None else str(value).strip() for value in rows[0]]
    blocks.append(SourceBlock(
        document_id=document_id, type="table_header", page=region.page,
        block_id=f"{parent_id}-header", parent_block_id=parent_id,
        content={"headers": headers}, coordinates=region.coordinates,
        extraction_method=methods, confidence=parent.confidence,
        validation_status=parent.validation_status, errors=list(parent.errors), warnings=list(parent.warnings),
        provenance=_provenance(source_hash, region, lines, image),
    ))
    for row_index, row in enumerate(rows[1:], 1):
        row_owner = "" if not row or row[0] is None else str(row[0]).strip()
        for column_index, value in enumerate(row[1:], 1):
            column_owner = headers[column_index] if column_index < len(headers) else ""
            ownership_errors = []
            if not row_owner:
                ownership_errors.append("missing row owner")
            if not column_owner:
                ownership_errors.append("missing column owner")
            blocks.append(SourceBlock(
                document_id=document_id, type="table_record", page=region.page,
                block_id=f"{parent_id}-r{row_index:03d}-c{column_index:03d}", parent_block_id=parent_id,
                content={
                    "table_title": table_title, "period": None,
                    "section": row_sections[row_index] if row_index < len(row_sections) else None,
                    "row": row_owner,
                    "column": column_owner, "value": value,
                    "value_state": "blank" if value is None or str(value).strip() == "" else "dash" if str(value).strip() in {"-", "–", "—"} else "present",
                },
                coordinates=region.coordinates, extraction_method=methods,
                confidence=max(0.0, parent.confidence - (0.12 if ownership_errors else 0.0)),
                validation_status="passed" if not ownership_errors and native else "needs_review",
                errors=ownership_errors, warnings=list(warnings),
                provenance=_provenance(source_hash, region, lines, image),
            ))
    return blocks


def _table_total_reconciliation(rows: list[list[Any]]) -> dict[str, Any] | None:
    """Reconcile generic subtotal rows against the final total wherever values are comparable."""
    if len(rows) < 4:
        return None

    total_rows = [
        row for row in rows[1:]
        if row and re.match(r"^\s*(?:grand\s+)?total\b", str(row[0] or ""), re.I)
    ]
    if len(total_rows) < 3:
        return None
    subtotal_rows, grand_total = total_rows[:-1], total_rows[-1]

    def number(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text or text in {"-", "–", "—"}:
            return None
        negative = text.startswith("(") and text.endswith(")")
        match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
        if not match:
            return None
        parsed = float(match.group().replace(",", ""))
        return -abs(parsed) if negative else parsed

    checks = []
    width = max(len(row) for row in rows)
    headers = rows[0]
    for column in range(1, width):
        parts = [number(row[column]) if column < len(row) else None for row in subtotal_rows]
        observed = number(grand_total[column]) if column < len(grand_total) else None
        if observed is None or any(part is None for part in parts):
            continue
        calculated = sum(part for part in parts if part is not None)
        tolerance = max(0.011, abs(observed) * 1e-8)
        passed = abs(calculated - observed) <= tolerance
        checks.append({
            "column_index": column,
            "column": str(headers[column] or "").strip() if column < len(headers) else "",
            "subtotal_values": parts,
            "observed_total": observed,
            "calculated_total": round(calculated, 5),
            "passed": passed,
        })
    if not checks:
        return None
    return {
        "passed": all(check["passed"] for check in checks),
        "subtotal_rows": [str(row[0]).strip() for row in subtotal_rows],
        "grand_total_row": str(grand_total[0]).strip(),
        "checks": checks,
    }


def _nearest_label(value_line: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    vx, vy, vw, vh = value_line["coordinates"]
    best = None
    best_distance = math.inf
    for candidate in candidates:
        cx, cy, cw, ch = candidate["coordinates"]
        distance = math.hypot((vx + vw / 2) - (cx + cw / 2), (vy + vh / 2) - (cy + ch / 2))
        if distance < best_distance:
            best, best_distance = candidate, distance
    return best


def _box_union(*boxes: list[float]) -> list[float]:
    valid = [box for box in boxes if isinstance(box, list) and len(box) == 4]
    if not valid:
        return [0.0, 0.0, 0.0, 0.0]
    x0 = min(box[0] for box in valid)
    y0 = min(box[1] for box in valid)
    x1 = max(box[0] + box[2] for box in valid)
    y1 = max(box[1] + box[3] for box in valid)
    return [x0, y0, x1 - x0, y1 - y0]


def _line_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    lx, ly, lw, lh = left["coordinates"]
    rx, ry, rw, rh = right["coordinates"]
    return math.hypot((lx + lw / 2) - (rx + rw / 2), (ly + lh / 2) - (ry + rh / 2))


def _visual_title(kind: str, lines: list[dict[str, Any]]) -> str | None:
    terms = MAP_TERMS if kind == "map" else CHART_TERMS
    matched = [line for line in lines if any(term in str(line["text"]).casefold() for term in terms)]
    candidates = [line for line in lines if not NUMBER.search(str(line["text"]))]
    pool = matched or candidates
    if not pool:
        return None
    return str(min(pool, key=lambda line: (line["coordinates"][1], line["coordinates"][0]))["text"]).strip()


def _numeric_value(text: str) -> tuple[float | None, str | None, float | None]:
    cleaned = text.replace(",", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None, None, None
    value = float(match.group())
    if "%" in cleaned:
        return value, "percent", value / 100.0
    if "$" in cleaned:
        multiplier = 1_000_000 if re.search(r"\b(?:m|mm|million)\b", cleaned, re.I) else 1_000_000_000 if re.search(r"\b(?:b|billion)\b", cleaned, re.I) else 1.0
        return value, "USD", value * multiplier
    if cleaned.lower().endswith("x"):
        return value, "multiple", value
    return value, None, value


def _infer_chart_type(
    region: Region, lines: list[dict[str, Any]], features: dict[str, Any],
    qwen_payload: dict[str, Any] | None,
) -> str:
    proposed = str((qwen_payload or {}).get("chart_type") or "").strip().lower()
    if proposed:
        return proposed
    hinted = str(region.metadata.get("chart_type_hint") or "").strip().lower()
    if hinted:
        return hinted
    percentage_count = sum("%" in str(line["text"]) for line in lines)
    image_count = len(region.metadata.get("image_indices", []))
    if image_count >= 4 and percentage_count >= 3:
        return "pie"
    if (
        int(features.get("point_candidates", 0)) >= 8
        and int(features.get("horizontal_axis_candidates", 0)) >= 1
        and int(features.get("vertical_axis_candidates", 0)) >= 1
    ):
        return "scatterplot"
    if int(features.get("bar_candidates", 0)) >= 3:
        return "bar"
    visible_text = " ".join(str(line.get("text", "")) for line in lines).casefold()
    if sum(bool(NUMBER.search(str(line.get("text", "")))) for line in lines) >= 2 and any(
        term in visible_text for term in {"investment", "portfolio", "assets", "properties"}
    ):
        return "kpi_panel"
    return "unknown"


def _nearest_mark(coordinates: list[float], marks: list[list[float]]) -> list[float] | None:
    if not marks:
        return None
    x, y, w, h = coordinates
    cx, cy = x + w / 2, y + h / 2
    return min(
        marks,
        key=lambda mark: math.hypot(cx - (mark[0] + mark[2] / 2), cy - (mark[1] + mark[3] / 2)),
    )


def _proximity_bindings(
    lines: list[dict[str, Any]], title: str | None, mark_boxes: list[list[float]],
) -> list[dict[str, Any]]:
    values = [line for line in lines if NUMBER.fullmatch(str(line["text"]).strip())]
    labels = [
        line for line in lines
        if not NUMBER.search(str(line["text"]))
        and str(line["text"]).strip() != (title or "")
        and len(str(line["text"]).strip()) >= 2
    ]
    pair_candidates = sorted(
        ((_line_distance(value, label), value, label) for value in values for label in labels),
        key=lambda item: item[0],
    )
    used_values: set[str] = set()
    used_labels: set[str] = set()
    bindings: list[dict[str, Any]] = []
    for distance, value, label in pair_candidates:
        value_id, label_id = value["evidence_id"], label["evidence_id"]
        if value_id in used_values or label_id in used_labels or distance > 150:
            continue
        used_values.add(value_id)
        used_labels.add(label_id)
        numeric_value, unit, normalized_value = _numeric_value(str(value["text"]))
        evidence_box = _box_union(label["coordinates"], value["coordinates"])
        bindings.append({
            "label": str(label["text"]).strip(),
            "value": str(value["text"]).strip(),
            "numeric_value": numeric_value,
            "unit": unit,
            "normalized_value": normalized_value,
            "label_evidence_id": label_id,
            "value_evidence_id": value_id,
            "label_coordinates": label["coordinates"],
            "value_coordinates": value["coordinates"],
            "visual_mark_coordinates": _nearest_mark(evidence_box, mark_boxes),
            "coordinates": evidence_box,
            "grounding_method": "unique OCR label-value proximity with raster mark ownership",
            "distance": round(distance, 5),
            "confidence": round(max(0.70, 0.96 - distance / 500), 5),
        })
    return sorted(bindings, key=lambda item: (item["value_coordinates"][1], item["value_coordinates"][0]))


_CHART_CATEGORY = re.compile(
    r"^(?:(?:19|20)\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-\d{2,4})$",
    re.I,
)
_CHART_VALUE = re.compile(
    r"^(?:[$€£]\s*)?[-+]?\d[\d,.]*(?:\s*(?:%|x|m|mm|million|b|billion|k))$",
    re.I,
)


def _center(box: list[float]) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3] / 2


def _bar_bindings(
    lines: list[dict[str, Any]], title: str | None, mark_boxes: list[list[float]],
) -> list[dict[str, Any]]:
    """Bind vertical bars to quantitative OCR and x-axis categories by x alignment."""
    marks: list[list[float]] = []
    for mark in sorted(mark_boxes, key=lambda box: _center(box)[0]):
        if not any(abs(_center(mark)[0] - _center(existing)[0]) <= 1.0 for existing in marks):
            marks.append(mark)
    if len(marks) < 3:
        return []
    baseline = statistics.median(mark[1] + mark[3] for mark in marks)
    highest_mark = min(mark[1] for mark in marks)
    categories = [
        line for line in lines
        if _CHART_CATEGORY.fullmatch(str(line["text"]).strip())
        and _center(line["coordinates"])[1] >= baseline - 15
    ]
    values = [
        line for line in lines
        if _CHART_VALUE.fullmatch(str(line["text"]).strip())
        and (_numeric_value(str(line["text"]))[1] in {"percent", "USD", "multiple"})
        and highest_mark - 35 <= _center(line["coordinates"])[1] <= baseline + 25
    ]
    series_labels = sorted([
        line for line in lines
        if _center(line["coordinates"])[1] > baseline
        and any(term in str(line["text"]).casefold() for term in {"outstanding", "coverage", "distribution"})
        and not _CHART_CATEGORY.fullmatch(str(line["text"]).strip())
    ], key=lambda line: line["coordinates"][0])
    assignments: list[tuple[list[float], dict[str, Any]]] = []
    available = list(marks)
    for value in sorted(values, key=lambda line: _center(line["coordinates"])[0]):
        if not available:
            break
        value_x, _ = _center(value["coordinates"])
        mark = min(available, key=lambda box: abs(_center(box)[0] - value_x))
        if abs(_center(mark)[0] - value_x) > 55:
            continue
        available.remove(mark)
        assignments.append((mark, value))

    category_groups: dict[str, list[list[float]]] = {}
    for mark in marks:
        if not categories:
            continue
        mark_x, _ = _center(mark)
        category = min(categories, key=lambda line: abs(_center(line["coordinates"])[0] - mark_x))
        if abs(_center(category["coordinates"])[0] - mark_x) <= 75:
            category_groups.setdefault(category["evidence_id"], []).append(mark)

    bindings: list[dict[str, Any]] = []
    for mark, value in sorted(assignments, key=lambda item: _center(item[0])[0]):
        if not categories:
            continue
        mark_x, _ = _center(mark)
        category = min(categories, key=lambda line: abs(_center(line["coordinates"])[0] - mark_x))
        category_dx = abs(_center(category["coordinates"])[0] - mark_x)
        value_dx = abs(_center(value["coordinates"])[0] - mark_x)
        if category_dx > 75:
            continue
        group = sorted(category_groups.get(category["evidence_id"], []), key=lambda box: _center(box)[0])
        series = None
        if len(group) > 1 and series_labels:
            rank = group.index(mark)
            series = str(series_labels[min(rank, len(series_labels) - 1)]["text"]).strip()
        numeric_value, unit, normalized_value = _numeric_value(str(value["text"]))
        confidence = max(0.70, 0.98 - value_dx / 300 - category_dx / 400)
        bindings.append({
            "label": str(category["text"]).strip(),
            "series": series,
            "value": str(value["text"]).strip(),
            "numeric_value": numeric_value,
            "unit": unit,
            "normalized_value": normalized_value,
            "label_evidence_id": category["evidence_id"],
            "value_evidence_id": value["evidence_id"],
            "label_coordinates": category["coordinates"],
            "value_coordinates": value["coordinates"],
            "visual_mark_coordinates": mark,
            "coordinates": _box_union(category["coordinates"], value["coordinates"], mark),
            "grounding_method": "x-aligned OCR value/category with PDF vector bar mark",
            "confidence": round(confidence, 5),
        })
    return bindings


def _visual_blocks(
    document_id: str, source_hash: str, region: Region, kind: str, classification_confidence: float,
    classification_warnings: list[str], lines: list[dict[str, Any]], features: dict[str, Any],
    image: Path, crop_path: Path, qwen_payload: dict[str, Any] | None,
    vision_features_ref: dict[str, str],
) -> list[SourceBlock]:
    parent_id = f"{region.region_id}-{kind.replace('_', '-')}"
    methods = ["RapidOCR", "PP-OCRv6", "OpenCV", "Python reconstruction"]
    if str(region.metadata.get("object_family") or "").startswith("pdf-vector"):
        methods.insert(-1, "PDF vector geometry")
    if qwen_payload is not None:
        methods.insert(-1, "Qwen3-VL visual grounding")
    labels = [str(line["text"]) for line in lines]
    title = _visual_title(kind, lines)
    chart_type = _infer_chart_type(region, lines, features, qwen_payload) if kind == "chart" else None
    parent_status = "passed" if classification_confidence >= 0.75 else "needs_review"
    parent = SourceBlock(
        document_id=document_id, type=kind, page=region.page, block_id=parent_id, parent_block_id=None,
        content={
            "title": title, "labels": labels,
            **({"chart_type": chart_type} if kind == "chart" else {}),
            "vision_summary": _vision_summary(features),
            "vision_features_ref": vision_features_ref,
            "region_image": str(crop_path),
        },
        coordinates=region.coordinates, extraction_method=methods, confidence=classification_confidence,
        validation_status=parent_status,
        errors=[] if labels or kind in {"unclassified_visual", "photograph", "decoration"} else ["no visible labels recovered"],
        warnings=classification_warnings,
        provenance=_provenance(source_hash, region, lines, image),
    )
    blocks = [parent]
    if kind not in {"chart", "map"}:
        return blocks
    bindings = list(qwen_payload.get("bindings", [])) if qwen_payload else []
    if not bindings:
        if chart_type == "bar":
            bindings = _bar_bindings(lines, title, region.metadata.get("member_coordinates", []))
        elif chart_type in {"scatterplot", "kpi_panel"}:
            bindings = []
            parent.warnings.append(
                "scatterplot points require axis-calibrated reconstruction"
                if chart_type == "scatterplot"
                else "KPI panel requires explicit label-value ownership reconstruction"
            )
        else:
            bindings = _proximity_bindings(lines, title, region.metadata.get("member_coordinates", []))
    if kind == "chart" and chart_type == "pie":
        percentage_total = sum(
            float(binding["numeric_value"])
            for binding in bindings
            if binding.get("unit") == "percent" and binding.get("numeric_value") is not None
        )
        parent.content["percentage_total"] = round(percentage_total, 5)
        parent.content["percentage_total_reconciles"] = 98.0 <= percentage_total <= 102.0
        if len(bindings) >= 3 and parent.content["percentage_total_reconciles"]:
            parent.validation_status = "passed"
            parent.confidence = max(parent.confidence, 0.90)
        else:
            parent.validation_status = "needs_review"
            parent.warnings.append("pie observations do not reconcile to a complete 100% allocation")
    expected_marks = region.metadata.get("expected_mark_count")
    if kind == "chart" and isinstance(expected_marks, int) and expected_marks > 0:
        parent.content["expected_observation_count"] = expected_marks
        parent.content["emitted_observation_count"] = len(bindings)
        parent.content["observation_completeness"] = round(len(bindings) / expected_marks, 5)
        if chart_type == "bar" and len(bindings) != expected_marks:
            parent.validation_status = "needs_review"
            parent.warnings.append(
                f"reconstructed {len(bindings)} of {expected_marks} detected vector bars"
            )
    child_type = "chart_observation" if kind == "chart" else "map_binding"
    for index, binding in enumerate(bindings, 1):
        label = str(binding.get("label") or binding.get("category") or binding.get("geography") or "").strip()
        value = str(binding.get("value") or "").strip()
        errors = []
        if not label:
            errors.append("binding has no owner label")
        if not value:
            errors.append("binding has no value")
        content = {
            "chart_title" if kind == "chart" else "map_title": title,
            "value": value,
            "numeric_value": binding.get("numeric_value"),
            "normalized_value": binding.get("normalized_value"),
            "unit": binding.get("unit"),
            "label_evidence_id": binding.get("label_evidence_id"),
            "value_evidence_id": binding.get("value_evidence_id"),
            "label_coordinates": binding.get("label_coordinates"),
            "value_coordinates": binding.get("value_coordinates"),
            "visual_mark_coordinates": binding.get("visual_mark_coordinates"),
            "grounding_method": binding.get("grounding_method"),
        }
        if kind == "chart":
            content.update({"chart_type": chart_type, "series": binding.get("series"), "category": label})
        else:
            content["geography"] = label
        selected_lines = [
            line for line in lines
            if line["evidence_id"] in {binding.get("label_evidence_id"), binding.get("value_evidence_id")}
        ]
        grounded = qwen_payload is not None or bool(selected_lines and binding.get("visual_mark_coordinates"))
        confidence = float(binding.get("confidence", 0.65 if grounded else 0.48))
        blocks.append(SourceBlock(
            document_id=document_id, type=child_type, page=region.page,
            block_id=f"{parent_id}-{child_type}-{index:03d}", parent_block_id=parent_id,
            content=content, coordinates=binding.get("visual_mark_coordinates") or binding.get("coordinates") or region.coordinates,
            extraction_method=methods, confidence=confidence,
            validation_status="passed" if grounded and not errors and confidence >= 0.70 else "needs_review",
            errors=errors, warnings=[] if grounded else ["nearest-label binding requires visual review"],
            provenance=_provenance(source_hash, region, selected_lines or lines, image),
        ))
    if not bindings:
        parent.validation_status = "needs_review"
        parent.warnings.append("no owned visual observations reconstructed")
    return blocks


def _route_and_extract_page(
    document_id: str, source_hash: str, inspection: PageInspection, image: Path,
    ocr_page: dict[str, Any], crops: Path, native_threshold: float,
    route_threshold: float, qwen: QwenVisionClient | None,
    diagnostics: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    page_lines = ocr_page.get("lines", [])
    region_results = ocr_page.get("regions", {})
    blocks: list[SourceBlock] = []
    assigned: set[str] = set()
    route_errors: list[str] = []
    for region in inspection.regions:
        lines = [line for line in page_lines if _contains(region.coordinates, line)]
        assigned.update(line["evidence_id"] for line in lines)
        features = region_results.get(region.region_id, {}).get("vision_features", {})
        if region.kind == "normal_text":
            blocks.append(_text_block(document_id, source_hash, inspection, region, lines, image, native_threshold))
            continue
        if region.kind == "table":
            blocks.extend(_table_blocks(document_id, source_hash, region, lines, image))
            continue
        if region.kind == "decoration":
            crop_path = crops / f"{region.region_id}.png"
            _crop(image, region.coordinates, crop_path)
            vision_features_ref = _write_vision_diagnostic(
                diagnostics, document_id, source_hash, region, features,
            )
            blocks.extend(_visual_blocks(
                document_id, source_hash, region, "decoration", region.confidence, [], lines,
                features, image, crop_path, None, vision_features_ref,
            ))
            continue
        kind, confidence, warnings = _classify_visual(region, lines, features)
        qwen_payload = None
        crop_path = crops / f"{region.region_id}.png"
        _crop(image, region.coordinates, crop_path)
        vision_features_ref = _write_vision_diagnostic(
            diagnostics, document_id, source_hash, region, features,
        )
        if qwen and confidence < route_threshold:
            try:
                qwen_payload = qwen.analyze(crop_path, (
                    "You are a document-region classifier. Return exactly one JSON object with exactly four "
                    "keys: type, confidence, chart_type, bindings. type must be exactly one of normal_text, "
                    "table, chart, map, photograph, unclassified_visual. confidence must be a JSON number from "
                    "zero to one. chart_type must be a short string or null. bindings must be a JSON array. "
                    "Do not return a transcription, a label-only object, Markdown, or commentary. Add bindings "
                    "only for clearly visible label and value pairs; otherwise return an empty array."
                ))
                proposed = str(qwen_payload.get("type", "")).strip().lower()
                if proposed in {"normal_text", "table", "chart", "map", "photograph", "unclassified_visual"}:
                    kind = proposed
                    confidence = float(qwen_payload.get("confidence", confidence))
                    region.classification_method = "Qwen3-VL visual classification"
                    warnings = [
                        warning for warning in warnings
                        if warning not in {
                            "visual type could not be classified confidently",
                            "visual classification is geometry-only",
                        }
                    ]
                else:
                    warnings.append("Qwen3-VL returned an unsupported region type")
            except Exception as exc:
                warnings.append(f"Qwen3-VL unavailable: {type(exc).__name__}: {exc}")
                route_errors.append(f"{region.region_id}: {warnings[-1]}")
        if kind == "normal_text":
            region.native_text = region.native_text or _ocr_text(lines)
            blocks.append(_text_block(document_id, source_hash, inspection, region, lines, image, 1.1))
        elif kind == "table":
            blocks.extend(_table_blocks(document_id, source_hash, region, lines, image))
        else:
            blocks.extend(_visual_blocks(
                document_id, source_hash, region, kind, confidence, warnings, lines, features,
                image, crop_path, qwen_payload, vision_features_ref,
            ))
    block_dicts = [block.as_dict() for block in blocks]
    errors = validate_source_blocks(block_dicts)
    unassigned = [
        {"evidence_id": line["evidence_id"], "reason": "outside detected regions", "evidence": line}
        for line in page_lines if line["evidence_id"] not in assigned
    ]
    return block_dicts, unassigned, errors + route_errors


def run_pipeline(
    pdf: Path, output: Path, pages_spec: str | None = None, dpi: int = 200,
    native_threshold: float = 0.78, route_threshold: float = 0.72,
    skip_ocr: bool = False, rapidocr_python: Path | None = None,
    rapidocr_models: Path | None = None, pdftoppm: Path | None = None,
    qwen_endpoint: str | None = None, qwen_model: str = "Qwen3-VL",
) -> Path:
    pdf = pdf.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Output must not exist; runs are immutable: {output}")
    output.mkdir(parents=True)
    source_dir = output / "source"
    images_dir = output / "page-images"
    crops_dir = output / "region-images"
    blocks_dir = output / "source-blocks"
    inspection_dir = output / "inspection"
    diagnostics_dir = output / "diagnostics" / "vision-features"
    work_dir = output / "work"
    for path in (source_dir, images_dir, crops_dir, blocks_dir, inspection_dir, diagnostics_dir, work_dir):
        path.mkdir(parents=True)
    source_hash = _sha256(pdf)
    document_id = f"{_slug(pdf.stem)}-{source_hash[:12]}"
    page_count = len(PdfReader(pdf).pages)
    pages = parse_pages(pages_spec, page_count)
    manifest: dict[str, Any] = {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "status": "running", "created_utc": _now(), "document_id": document_id,
        "source_filename": pdf.name, "source_sha256": source_hash,
        "page_count": page_count, "selected_pages": pages, "dpi": dpi,
        "coordinate_system": "top-left normalized xywh 0..1000",
        "thresholds": {"native_text_usability": native_threshold, "routing_confidence": route_threshold},
        "vision_model": {
            "enabled": bool(qwen_endpoint),
            "provider": "OpenAI-compatible" if qwen_endpoint else None,
            "endpoint": qwen_endpoint,
            "model": qwen_model if qwen_endpoint else None,
            "routing": "visual regions below routing_confidence",
        },
        "runtime": {
            "python": sys.version,
            "pdfplumber": importlib.metadata.version("pdfplumber"),
            "pypdf": importlib.metadata.version("pypdf"),
            "pillow": importlib.metadata.version("Pillow"),
            "pipeline": f"dealsynq-ocr-pipeline/{PIPELINE_VERSION}",
        },
        "stages": [],
    }
    _write_json(output / "manifest.json", manifest)
    try:
        copied = source_dir / pdf.name
        shutil.copy2(pdf, copied)
        if _sha256(copied) != source_hash:
            raise RuntimeError("Preserved source copy hash mismatch")
        manifest["preserved_source"] = str(copied)
        manifest["stages"].append({"name": "ingestion", "status": "complete", "finished_utc": _now()})
        _write_json(output / "manifest.json", manifest)

        inspections, pdf_metadata = inspect_pdf(pdf)
        selected_inspections = [inspection for inspection in inspections if inspection.page in pages]
        inspection_path = inspection_dir / "document-inspection.json"
        _write_json(inspection_path, {
            "schema_version": INSPECTION_SCHEMA_VERSION,
            "document_id": document_id, "source_sha256": source_hash,
            "page_count": page_count, "selected_pages": pages, "pdf_metadata": pdf_metadata,
            "pages": [inspection.as_dict() for inspection in selected_inspections],
        })
        manifest["inspection"] = {
            "path": str(inspection_path.relative_to(output)).replace("\\", "/"),
            "sha256": _sha256(inspection_path),
            "schema_version": INSPECTION_SCHEMA_VERSION,
        }
        manifest["stages"].append({"name": "pdf-inspection", "status": "complete", "finished_utc": _now()})

        rendered = _render_pages(pdf, pages, images_dir, dpi, pdftoppm)
        render_executable = pdftoppm or (Path(shutil.which("pdftoppm")) if shutil.which("pdftoppm") else None)
        render_version = None
        if render_executable:
            version_result = subprocess.run(
                [str(render_executable), "-v"], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            render_version = (version_result.stdout or version_result.stderr).strip().splitlines()[:1]
        manifest["rendering"] = {
            "engine": str(render_executable) if render_executable else None,
            "version": render_version[0] if render_version else None,
            "dpi": dpi, "format": "PNG",
        }
        manifest["stages"].append({"name": "rendering", "status": "complete", "finished_utc": _now()})
        jobs = [{
            "page": inspection.page, "image": str(rendered[inspection.page]),
            "regions": [{"region_id": region.region_id, "coordinates": region.coordinates} for region in inspection.regions],
        } for inspection in selected_inspections]
        if skip_ocr:
            ocr_result = {"engine": None, "pages": [{"page": page, "lines": [], "regions": {}} for page in pages]}
            manifest["ocr"] = {"enabled": False, "reason": "--skip-ocr"}
        else:
            ocr_result = run_rapidocr_worker(jobs, work_dir, rapidocr_python, rapidocr_models)
            manifest["ocr"] = {
                "enabled": True, "engine": ocr_result.get("engine"),
                "rapidocr_version": ocr_result.get("rapidocr_version"),
                "opencv_version": ocr_result.get("opencv_version"),
            }
        manifest["stages"].append({"name": "ocr-and-geometry", "status": "complete", "finished_utc": _now()})

        qwen = QwenVisionClient(qwen_endpoint, qwen_model) if qwen_endpoint else None
        ocr_by_page = {int(item["page"]): item for item in ocr_result.get("pages", [])}
        page_records = []
        type_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        collection_errors: list[str] = []
        for inspection in selected_inspections:
            ocr_page = ocr_by_page.get(inspection.page, {"lines": [], "regions": {}})
            blocks, unassigned, errors = _route_and_extract_page(
                document_id, source_hash, inspection, rendered[inspection.page],
                ocr_page,
                crops_dir, native_threshold, route_threshold, qwen, diagnostics_dir,
            )
            for block in blocks:
                type_counts[block["type"]] += 1
                status_counts[block["validation"]["status"]] += 1
            page_payload = {
                "schema_version": PAGE_SCHEMA_VERSION, "document_id": document_id,
                "source_sha256": source_hash, "page": inspection.page,
                "blocks": blocks,
                "evidence_ledger": {
                    "rendered_page": str(rendered[inspection.page]),
                    "rendered_page_sha256": _sha256(rendered[inspection.page]),
                    "ocr_engine": ocr_result.get("engine"),
                    "ocr_lines": ocr_page.get("lines", []),
                },
                "unassigned_evidence": unassigned,
                "validation": {"valid": not errors, "errors": errors},
            }
            page_path = blocks_dir / f"page-{inspection.page:03d}.json"
            _write_json(page_path, page_payload)
            page_records.append({
                "page": inspection.page, "file": page_path.name, "sha256": _sha256(page_path),
                "block_count": len(blocks), "unassigned_evidence_count": len(unassigned),
                "valid": not errors,
            })
            collection_errors.extend(f"page {inspection.page}: {error}" for error in errors)
        collection_manifest = {
            "schema_version": COLLECTION_SCHEMA_VERSION, "document_id": document_id,
            "source_filename": pdf.name, "source_sha256": source_hash, "page_count": page_count,
            "selected_pages": pages, "pages": page_records, "block_counts_by_type": dict(sorted(type_counts.items())),
            "validation_counts": dict(sorted(status_counts.items())),
            "validation": {"valid": not collection_errors, "errors": collection_errors},
            "semantic_interpretation_performed": False,
        }
        _write_json(blocks_dir / "document-manifest.json", collection_manifest)
        manifest["stages"].extend([
            {"name": "region-routing-and-extraction", "status": "complete", "finished_utc": _now()},
            {"name": "reconstruction-and-validation", "status": "complete", "finished_utc": _now()},
            {"name": "unified-source-block-collection", "status": "complete", "finished_utc": _now()},
        ])
        manifest["status"] = "complete"
        manifest["finished_utc"] = _now()
        manifest["source_blocks_manifest"] = str(blocks_dir / "document-manifest.json")
        manifest["validation"] = collection_manifest["validation"]
        _write_json(output / "manifest.json", manifest)
        return output
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["finished_utc"] = _now()
        manifest["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        _write_json(output / "manifest.json", manifest)
        raise

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
import difflib
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
import unicodedata

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
    "puerto rico",
}
PIPELINE_VERSION = "0.6.0"
PAGE_SCHEMA_VERSION = "unified-source-page/4.0"
COLLECTION_SCHEMA_VERSION = "unified-source-collection/3.0"
INSPECTION_SCHEMA_VERSION = "pdf-page-inspection/1.0"
INSPECTION_INDEX_SCHEMA_VERSION = "pdf-inspection-index/1.0"
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


def _exclusive_region_lines(
    regions: list[Region], page_lines: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Assign every OCR line to at most one logical region.

    PDF image objects often overlap semantic text and repeated footer artwork.
    A single evidence item must not silently support multiple root blocks.
    """
    result = {region.region_id: [] for region in regions}
    # A real visual object owns the text drawn inside it (chart labels, values,
    # map annotations, and similar overlays). Decoration remains lower than
    # normal text so a background image cannot steal prose evidence.
    priority = {"table": 6, "visual": 5, "normal_text": 4, "unknown": 2, "decoration": 1}
    for line in page_lines:
        candidates = [region for region in regions if _contains(region.coordinates, line)]
        if not candidates:
            continue
        owner = max(
            candidates,
            key=lambda region: (
                priority.get(region.kind, 2),
                -region.coordinates[2] * region.coordinates[3],
                region.confidence,
            ),
        )
        result[owner.region_id].append(line)
    return result


def _augment_native_visual_evidence(
    inspection: PageInspection, page_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add non-duplicate native positioned words for detected visual regions."""
    result = list(page_lines)
    seen_candidates: set[tuple[str, tuple[float, ...]]] = set()
    native_index = 0
    for region in inspection.regions:
        if region.kind != "visual":
            continue
        candidates = list(region.metadata.get("native_visual_words", []))
        if str(region.metadata.get("chart_type_hint") or "").casefold() == "bar":
            candidates.extend(_reconstruct_vertical_values(candidates))
        candidates.extend(_reconstruct_vertical_words(candidates))
        for candidate in candidates:
            text = str(candidate.get("text", "")).strip()
            coordinates = [float(value) for value in candidate.get("coordinates", [])]
            if not text or len(coordinates) != 4:
                continue
            key = (text.casefold(), tuple(round(value, 2) for value in coordinates))
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            cx, cy = _center(coordinates)
            duplicate = any(
                str(line.get("text", "")).strip().casefold() == text.casefold()
                and abs(cx - _center(line["coordinates"])[0]) <= 15
                and abs(cy - _center(line["coordinates"])[1]) <= 45
                for line in result
            )
            if duplicate:
                continue
            native_index += 1
            result.append({
                "evidence_id": f"p{inspection.page:03d}-native-{native_index:04d}",
                "text": text, "confidence": 1.0, "coordinates": coordinates,
                "evidence_source": "native_pdf_positioned_word",
            })
    return result


def _augment_native_table_evidence(
    inspection: PageInspection, page_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = list(page_lines)
    for region in inspection.regions:
        if region.kind != "table":
            continue
        rows = region.metadata.get("rows") or []
        coordinates = region.metadata.get("cell_coordinates") or []
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                text = str(value or "").strip()
                cell_coordinates = (
                    coordinates[row_index][column_index]
                    if row_index < len(coordinates) and column_index < len(coordinates[row_index])
                    else None
                )
                if not text or not cell_coordinates:
                    continue
                result.append({
                    "evidence_id": f"{region.region_id}-native-table-r{row_index:03d}-c{column_index:03d}",
                    "text": text, "confidence": 1.0, "coordinates": cell_coordinates,
                    "evidence_source": "native_pdf_table_cell",
                })
    return result


def _reconstruct_vertical_values(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reassemble rotated numeric labels split by native PDF extraction.

    Some PDF generators encode a rotated percentage as vertically stacked
    number fragments plus a percent sign. Geometry, rather than a value list,
    list, determines the reconstructed label.
    """
    tokens = [
        item for item in candidates
        if re.fullmatch(r"[\d.%]+", str(item.get("text", "")).strip())
        and len(item.get("coordinates", [])) == 4
    ]
    by_x: list[list[dict[str, Any]]] = []
    for token in sorted(tokens, key=lambda item: (_center(item["coordinates"])[0], item["coordinates"][1])):
        cx, _ = _center(token["coordinates"])
        group = next((group for group in by_x if abs(_center(group[0]["coordinates"])[0] - cx) <= 4), None)
        if group is None:
            by_x.append([token])
        else:
            group.append(token)
    reconstructed = []
    for x_group in by_x:
        clusters: list[list[dict[str, Any]]] = []
        for token in sorted(x_group, key=lambda item: item["coordinates"][1]):
            if not clusters:
                clusters.append([token])
                continue
            previous = clusters[-1][-1]["coordinates"]
            gap = token["coordinates"][1] - (previous[1] + previous[3])
            if gap <= 45:
                clusters[-1].append(token)
            else:
                clusters.append([token])
        for cluster in clusters:
            texts = [str(item["text"]).strip() for item in cluster]
            if "%" not in texts or not any(any(char.isdigit() for char in text) for text in texts):
                continue
            ordered = sorted(cluster, key=lambda item: item["coordinates"][1], reverse=True)
            value = "".join(
                str(item["text"])[::-1] if str(item["text"]).isdigit() else str(item["text"])
                for item in ordered
            )
            if not re.fullmatch(r"\d{1,3}(?:\.\d+)?%", value):
                continue
            reconstructed.append({
                "text": value, "coordinates": _box_union(*(item["coordinates"] for item in cluster)),
                "confidence": 1.0, "evidence_source": "native_pdf_rotated_value_reconstruction",
            })
    return reconstructed


def _reconstruct_vertical_words(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reassemble vertically encoded units and labels from positioned PDF tokens."""
    tokens = [
        item for item in candidates
        if re.fullmatch(r"[A-Za-z]{1,3}", str(item.get("text", "")).strip())
        and len(item.get("coordinates", [])) == 4
    ]
    x_groups: list[list[dict[str, Any]]] = []
    for token in sorted(tokens, key=lambda item: (_center(item["coordinates"])[0], item["coordinates"][1])):
        cx, _ = _center(token["coordinates"])
        group = next((items for items in x_groups if abs(_center(items[0]["coordinates"])[0] - cx) <= 4), None)
        if group is None:
            x_groups.append([token])
        else:
            group.append(token)
    reconstructed: list[dict[str, Any]] = []
    for x_group in x_groups:
        clusters: list[list[dict[str, Any]]] = []
        for token in sorted(x_group, key=lambda item: item["coordinates"][1]):
            if not clusters:
                clusters.append([token])
                continue
            previous = clusters[-1][-1]["coordinates"]
            gap = token["coordinates"][1] - (previous[1] + previous[3])
            if gap <= 12:
                clusters[-1].append(token)
            else:
                clusters.append([token])
        for cluster in clusters:
            if len(cluster) < 3:
                continue
            text = "".join(str(item["text"]).strip() for item in cluster)
            if not 3 <= len(text) <= 16:
                continue
            reconstructed.append({
                "text": text,
                "coordinates": _box_union(*(item["coordinates"] for item in cluster)),
                "confidence": 1.0,
                "evidence_source": "native_pdf_vertical_word_reconstruction",
            })
    return reconstructed


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


def _fold_token(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in value if not unicodedata.combining(character))


def _hybrid_text(native: str, ocr: str) -> str:
    """Use complete OCR wording while repairing OCR spelling from native PDF tokens."""
    native_words = re.findall(r"[\w’'-]+", native, re.UNICODE)
    by_folded: dict[str, list[str]] = {}
    for word in native_words:
        by_folded.setdefault(_fold_token(word), []).append(word)

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        exact = by_folded.get(_fold_token(word))
        if exact:
            return exact[0]
        candidates = difflib.get_close_matches(_fold_token(word), by_folded, n=1, cutoff=0.90)
        return by_folded[candidates[0]][0] if candidates else word

    return re.sub(r"[\w’'-]+", replace, ocr, flags=re.UNICODE)


def _text_structure(raw: str) -> dict[str, Any] | None:
    """Recover paragraph and bullet semantics from line breaks without document vocabulary."""
    normalized_paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n", raw)
        if paragraph.strip()
    ]
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines() if line.strip()]
    bullet_indexes = [index for index, line in enumerate(lines) if re.match(r"^(?:[-•▪‣]|\d+[.)])\s+", line)]
    if not bullet_indexes:
        if len(normalized_paragraphs) <= 1:
            return None
        return {"paragraphs": normalized_paragraphs, "lists": []}
    first = bullet_indexes[0]
    intro_index = first - 1 if first and lines[first - 1].endswith(":") else None
    prose_lines = lines[:intro_index if intro_index is not None else first]
    paragraphs: list[str] = []
    current: list[str] = []
    for line in prose_lines:
        current.append(line)
        if re.search(r"[.!?][\"'’)]?$", line):
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    items = [
        re.sub(r"^(?:[-•▪‣]|\d+[.)])\s+", "", lines[index]).strip()
        for index in bullet_indexes
    ]
    return {
        "paragraphs": paragraphs,
        "lists": [{
            "intro": lines[intro_index] if intro_index is not None else None,
            "ordered": bool(re.match(r"^\d+[.)]\s+", lines[first])),
            "items": items,
        }],
    }


def _classify_visual(region: Region, lines: list[dict[str, Any]], features: dict[str, Any]) -> tuple[str, float, list[str]]:
    text = " ".join(str(line["text"]) for line in lines).lower()
    warnings: list[str] = []
    visual_hint = str(region.metadata.get("visual_hint") or "").strip().lower()
    if visual_hint == "chart":
        return "chart", max(0.80, float(region.confidence)), warnings
    if visual_hint == "photograph":
        return "photograph", max(0.82, float(region.confidence)), warnings
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


def _block_type_for_text(region: Region, candidate_text: str | None = None) -> str:
    text = clean_text(region.native_text if candidate_text is None else candidate_text)
    font = float(region.metadata.get("median_font_size", 0) or 0)
    word_count = int(region.metadata.get("word_count", len(text.split())) or 0)
    y = region.coordinates[1]
    if y >= 890 or (font and font <= 8 and y >= 820):
        return "footnote"
    if "@" in text or re.search(r"\b\d{3}[-.) ]\d{3}[- ]\d{4}\b", text):
        return "contact"
    if word_count <= 14 and not text.endswith((".", ",", ";", ":")) and (font >= 14 or text.isupper() or text.istitle()):
        return "heading"
    return "text"


def _semantic_role_for_text(
    block_type: str, text: str, coordinates: list[float], page_number: int, nested: bool = False,
) -> str:
    folded = text.casefold()
    legal_signals = {
        "confidential", "important note", "legal notice", "disclaimer", "offer to sell",
        "solicitation", "securities", "offering materials", "terms and conditions",
    }
    if sum(signal in folded for signal in legal_signals) >= 2:
        return "legal_notice"
    if block_type == "heading":
        if nested:
            return "panel_heading"
        if coordinates[1] <= 220:
            return "document_title" if page_number == 1 else "page_title"
        return "section_heading"
    if block_type == "footnote":
        return "footnote"
    if block_type == "contact":
        return "contact_information"
    return "body_text"


def _line_box(lines: list[dict[str, Any]]) -> list[float]:
    x0 = min(float(line["coordinates"][0]) for line in lines)
    y0 = min(float(line["coordinates"][1]) for line in lines)
    x1 = max(float(line["coordinates"][0]) + float(line["coordinates"][2]) for line in lines)
    y1 = max(float(line["coordinates"][1]) + float(line["coordinates"][3]) for line in lines)
    return [x0, y0, x1 - x0, y1 - y0]


def _split_visual_text_lines(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split image-backed text using vertical whitespace, independent of document wording."""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda line: (float(line["coordinates"][1]), float(line["coordinates"][0])))
    heights = [max(1.0, float(line["coordinates"][3])) for line in ordered]
    median_height = statistics.median(heights)
    groups: list[list[dict[str, Any]]] = [[ordered[0]]]
    previous_bottom = float(ordered[0]["coordinates"][1]) + float(ordered[0]["coordinates"][3])
    for line in ordered[1:]:
        top = float(line["coordinates"][1])
        gap = top - previous_bottom
        if gap > max(10.0, median_height * 0.72):
            groups.append([line])
        else:
            groups[-1].append(line)
        previous_bottom = max(previous_bottom, top + float(line["coordinates"][3]))
    return groups


def _looks_like_brand_mark(
    lines: list[dict[str, Any]], page_lines: list[dict[str, Any]],
    document_token_pages: Counter[str] | None = None,
) -> bool:
    """Recognize short logo text from repetition or strong page-corner placement."""
    if not 1 <= len(lines) <= 3:
        return False
    text = " ".join(str(line.get("text", "")) for line in lines).strip()
    tokens = {
        _fold_token(token) for token in re.findall(r"[^\W\d_]{2,}", text, re.UNICODE)
        if _fold_token(token)
    }
    if not 1 <= len(tokens) <= 8 or NUMBER.search(text):
        return False
    letters = [character for character in text if character.isalpha()]
    if not letters or sum(character.isupper() for character in letters) / len(letters) < 0.72:
        return False
    own_ids = {str(line.get("evidence_id")) for line in lines}
    elsewhere = " ".join(
        str(line.get("text", "")) for line in page_lines
        if str(line.get("evidence_id")) not in own_ids
    ).casefold()
    repeated = sum(bool(re.search(rf"\b{re.escape(token)}\b", elsewhere)) for token in tokens)
    page_repetition = repeated / len(tokens) >= 0.75
    document_repetition = bool(document_token_pages) and (
        sum(document_token_pages.get(token, 0) >= 2 for token in tokens) / len(tokens) >= 0.75
    )
    x, y, width, height = _line_box(lines)
    in_vertical_corner_band = y >= 800 or y + height <= 200
    in_horizontal_corner_band = x <= 400 or x + width >= 600
    corner_signature = width <= 360 and height <= 140 and in_vertical_corner_band and in_horizontal_corner_band
    return page_repetition or document_repetition or corner_signature


def _inline_heading_subsection_blocks(
    document_id: str, source_hash: str, inspection: PageInspection, region: Region,
    lines: list[dict[str, Any]], image: Path, native_threshold: float,
) -> list[SourceBlock] | None:
    """Split repeated inline all-caps labels into semantic subsection trees."""
    native_lines = [line.strip() for line in region.native_text.splitlines() if line.strip()]
    heading_pattern = re.compile(r"^([A-Z][A-Z0-9 &/'-]{2,}?):\s*(.*)$")
    headings = [
        (index, match.group(1).strip(), match.group(2).strip())
        for index, line in enumerate(native_lines)
        if (match := heading_pattern.match(line))
    ]
    # Repetition is the structural evidence. A lone colon-led line can be ordinary prose.
    if len(headings) < 2:
        return None
    ordered_ocr = sorted(lines, key=lambda line: (float(line["coordinates"][1]), float(line["coordinates"][0])))
    if len(ordered_ocr) != len(native_lines):
        return None

    positive_steps = [
        float(current["coordinates"][1]) - float(previous["coordinates"][1])
        for previous, current in zip(ordered_ocr, ordered_ocr[1:])
        if float(current["coordinates"][1]) > float(previous["coordinates"][1])
    ]
    typical_step = statistics.median(positive_steps) if positive_steps else 0.0
    blocks: list[SourceBlock] = []
    for position, (start, heading_text, first_body_text) in enumerate(headings, 1):
        end = headings[position][0] if position < len(headings) else len(native_lines)
        section_ocr = [dict(line) for line in ordered_ocr[start:end]]
        if not section_ocr:
            return None
        body_lines = [first_body_text, *native_lines[start + 1:end]]
        body_parts: list[str] = []
        for line_index, body_line in enumerate(body_lines):
            if line_index and typical_step:
                previous = section_ocr[line_index - 1]
                current = section_ocr[line_index]
                step = float(current["coordinates"][1]) - float(previous["coordinates"][1])
                if step > typical_step * 1.35:
                    body_parts.append("")
            body_parts.append(body_line)
        body_native = "\n".join(body_parts).strip()
        if not body_native:
            return None

        first_ocr = str(section_ocr[0].get("text", ""))
        ocr_match = heading_pattern.match(first_ocr)
        ocr_heading = ocr_match.group(1).strip() if ocr_match else heading_text
        section_ocr[0]["text"] = ocr_match.group(2).strip() if ocr_match else first_ocr
        section_box = _line_box(section_ocr)
        first_box = [float(value) for value in ordered_ocr[start]["coordinates"]]
        heading_fraction = min(0.72, max(0.12, (len(heading_text) + 1) / max(1, len(first_ocr))))
        heading_box = [first_box[0], first_box[1], first_box[2] * heading_fraction, first_box[3]]
        group_id = f"{region.region_id}-subsection-{position:03d}"
        heading_id = f"{group_id}-heading"
        body_region = Region(
            region_id=f"{group_id}-body", page=region.page, kind="normal_text",
            coordinates=section_box, reading_order=region.reading_order + position,
            classification_method="inline-heading subsection reconstruction",
            confidence=region.confidence, native_text=body_native,
            metadata={
                "word_count": len(re.findall(r"\w+", body_native, re.UNICODE)),
                "source_bbox_points": region.metadata.get("source_bbox_points"),
            },
        )
        body = _text_block(
            document_id, source_hash, inspection, body_region, section_ocr, image, native_threshold,
        )
        body.parent_block_id = group_id
        body.hierarchy_depth = 1
        heading_agreement = _token_agreement(heading_text, ocr_heading)
        heading_confidence = min(
            inspection.native_text_quality,
            0.5 + heading_agreement / 2 if heading_agreement is not None else region.confidence,
        )
        heading = SourceBlock(
            document_id=document_id, type="heading", page=region.page, block_id=heading_id,
            content={
                "text": heading_text,
                "evidence_text": {
                    "selected": "native", "native": heading_text, "ocr": ocr_heading,
                    "token_agreement": heading_agreement,
                },
            },
            coordinates=heading_box,
            extraction_method=["native PDF text", "Python inline-heading reconstruction"],
            confidence=heading_confidence, validation_status="passed",
            provenance=_provenance(source_hash, body_region, [], image),
            semantic_role="section_heading", parent_block_id=group_id,
            hierarchy_depth=1, heading_level=2,
        )
        group = SourceBlock(
            document_id=document_id, type="group", page=region.page, block_id=group_id,
            content={"role": "subsection", "child_block_ids": [heading_id, body.block_id]},
            coordinates=section_box,
            extraction_method=["Python inline-heading hierarchy"],
            confidence=min(heading.confidence, body.confidence),
            validation_status="passed" if body.validation_status == "passed" else "needs_review",
            warnings=[] if body.validation_status == "passed" else ["one or more child blocks require review"],
            provenance=_provenance(source_hash, body_region, [], image),
            semantic_role="document_subsection", child_block_ids=[heading_id, body.block_id],
        )
        blocks.extend([group, heading, body])
    return blocks


def _profile_biography_blocks(
    document_id: str, source_hash: str, inspection: PageInspection, region: Region,
    lines: list[dict[str, Any]], image: Path, native_threshold: float,
) -> list[SourceBlock] | None:
    """Split a name-and-role lead line from the biography that follows it."""
    native_lines = [line.strip() for line in region.native_text.splitlines() if line.strip()]
    if len(native_lines) < 2 or len(" ".join(native_lines[1:]).split()) < 20:
        return None
    heading_text = native_lines[0]
    if not re.fullmatch(r"[^.!?:]{2,80}\s+[–—-]\s+[^.!?:]{2,60}", heading_text):
        return None
    if not 2 <= len(heading_text.split()) <= 12:
        return None
    ordered_ocr = sorted(lines, key=lambda line: (float(line["coordinates"][1]), float(line["coordinates"][0])))
    if len(ordered_ocr) < 2:
        return None
    heading_ocr = ordered_ocr[0]
    body_ocr = ordered_ocr[1:]
    body_native = "\n".join(native_lines[1:])
    group_id = f"{region.region_id}-profile-biography"
    heading_id = f"{group_id}-heading"
    body_region = Region(
        region_id=f"{group_id}-body", page=region.page, kind="normal_text",
        coordinates=_line_box(body_ocr), reading_order=region.reading_order + 1,
        classification_method="name-role biography reconstruction",
        confidence=region.confidence, native_text=body_native,
        metadata={
            "word_count": len(re.findall(r"\w+", body_native, re.UNICODE)),
            "source_bbox_points": region.metadata.get("source_bbox_points"),
        },
    )
    body = _text_block(
        document_id, source_hash, inspection, body_region, body_ocr, image, native_threshold,
    )
    body.parent_block_id = group_id
    body.hierarchy_depth = 1
    heading_ocr_text = str(heading_ocr.get("text", "")).strip()
    agreement = _token_agreement(heading_text, heading_ocr_text)
    heading = SourceBlock(
        document_id=document_id, type="heading", page=region.page, block_id=heading_id,
        content={
            "text": heading_text,
            "evidence_text": {
                "selected": "native", "native": heading_text, "ocr": heading_ocr_text or None,
                "token_agreement": agreement,
            },
        },
        coordinates=[float(value) for value in heading_ocr["coordinates"]],
        extraction_method=["native PDF text", "RapidOCR", "Python name-role heading reconstruction"],
        confidence=min(inspection.native_text_quality, 0.5 + agreement / 2 if agreement is not None else region.confidence),
        validation_status="passed", provenance=_provenance(source_hash, region, [heading_ocr], image),
        semantic_role="profile_name_and_role", parent_block_id=group_id,
        hierarchy_depth=1, heading_level=2,
    )
    group = SourceBlock(
        document_id=document_id, type="group", page=region.page, block_id=group_id,
        content={"role": "profile_biography", "child_block_ids": [heading_id, body.block_id]},
        coordinates=region.coordinates, extraction_method=["Python profile-biography hierarchy"],
        confidence=min(heading.confidence, body.confidence),
        validation_status="passed" if body.validation_status == "passed" else "needs_review",
        warnings=[] if body.validation_status == "passed" else ["one or more child blocks require review"],
        provenance=_provenance(source_hash, region, [], image), semantic_role="document_subsection",
        child_block_ids=[heading_id, body.block_id],
    )
    return [group, heading, body]


def _parse_contact_details(raw_text: str, normalized: str) -> dict[str, Any]:
    """Recover common contact fields while preserving the original text as evidence."""
    lines = [re.sub(r"\s+", " ", line).strip(" |│") for line in raw_text.splitlines() if line.strip()]
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", normalized)
    phone_match = re.search(r"\b(?:\+?1[-. )]*)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b", normalized)
    website_match = re.search(
        r"(?<![@\w])(?:https?://|www\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}(?:/[^\s|│]*)?",
        normalized, re.I,
    )
    organization = None
    suffix = re.compile(r"\b(?:LLC|L\.L\.C\.|INC\.?|CORP\.?|LTD\.?|LLP|LP|PLC)\b", re.I)
    for line in lines[:3]:
        if not any(character.isdigit() for character in line) and (
            suffix.search(line) or (line.isupper() and 1 <= len(line.split()) <= 10)
        ):
            organization = line.rstrip(",")
            break

    street = city = state = postal_code = None
    street_suffix = re.compile(
        r"\b(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|BOULEVARD|BLVD|"
        r"PARKWAY|PKWY|HIGHWAY|HWY|COURT|CT|CIRCLE|CIR|TRAIL|TRL|WAY|SUITE|STE)\b",
        re.I,
    )
    for index, line in enumerate(lines):
        if re.search(r"\d", line) and street_suffix.search(line):
            street = line
            if index + 1 < len(lines):
                locality = re.match(r"^(.*?)(?:,\s*|\s+)([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", lines[index + 1])
                if locality:
                    city, state, postal_code = locality.group(1).strip(), locality.group(2), locality.group(3)
            break
    address = None
    if any((street, city, state, postal_code)):
        address = {"street": street, "city": city, "state": state, "postal_code": postal_code}
    website = website_match.group(0) if website_match else None
    if website and not re.match(r"https?://", website, re.I):
        website = f"https://{website}"
    return {
        "name": None,
        "organization": organization,
        "address": address,
        "email": email_match.group(0) if email_match else None,
        "phone": phone_match.group(0) if phone_match else None,
        "website": website,
    }


def _provenance(source_hash: str, region: Region, ocr_lines: list[dict[str, Any]], image: Path) -> dict[str, Any]:
    return {
        "source_sha256": source_hash,
        "region_id": region.region_id,
        "classification_method": region.classification_method,
        "reading_order": region.reading_order,
        "source_bbox_points": region.metadata.get("source_bbox_points"),
        "ocr_evidence_ids": [line["evidence_id"] for line in ocr_lines],
        "rendered_page": f"page-images/{image.name}",
    }


def _text_block(
    document_id: str, source_hash: str, inspection: PageInspection, region: Region,
    lines: list[dict[str, Any]], image: Path, native_threshold: float,
) -> SourceBlock:
    visible_ocr = _ocr_text(lines)
    agreement = _token_agreement(region.native_text, visible_ocr)
    use_native = bool(region.native_text.strip()) and inspection.native_text_quality >= native_threshold
    selected = "native" if use_native else "ocr"
    warnings: list[str] = []
    if use_native and agreement is not None and agreement < 0.35:
        use_native = False
        selected = "ocr"
        warnings.append("native text disagrees with visible OCR; OCR selected")
    native_word_count = len(re.findall(r"\w+", region.native_text, re.UNICODE))
    ocr_word_count = len(re.findall(r"\w+", visible_ocr, re.UNICODE))
    if use_native and agreement is not None and agreement >= 0.35 and ocr_word_count >= native_word_count + 2:
        raw = _hybrid_text(region.native_text, visible_ocr)
        selected = "hybrid"
        use_native = False
    else:
        raw = region.native_text if use_native else visible_ocr
    normalized = clean_text(raw)
    if selected == "hybrid":
        methods = ["native PDF text", "RapidOCR", "PP-OCRv6", "Python token reconciliation", "Python layout normalization"]
    elif use_native:
        methods = ["native PDF text", "Python layout normalization"]
    else:
        methods = ["RapidOCR", "PP-OCRv6", "Python layout normalization"]
    errors = [] if normalized else ["no text recovered from region"]
    source_confidence = inspection.native_text_quality if selected in {"native", "hybrid"} else (
        sum(float(line["confidence"]) for line in lines) / len(lines) if lines else 0.0
    )
    confidence = min(source_confidence, 0.5 + agreement / 2) if agreement is not None else source_confidence
    block_type = _block_type_for_text(region, normalized)
    word_count = int(region.metadata.get("word_count", len(normalized.split())) or 0)
    if block_type == "text" and word_count <= 3:
        warnings.append("short text fragment may have been detached from an adjacent region")
    if (
        region.coordinates[2] >= 700 and inspection.possible_visual_regions >= 2 and word_count >= 20
        and region.classification_method not in {
            "visual-text-whitespace-segmentation",
            "name-role biography reconstruction",
        }
    ):
        warnings.append("wide text spans a mixed visual layout; reading order requires review")
    if "�" in normalized:
        warnings.append("text contains an invalid replacement character")
    content: dict[str, Any] = {
        "text": normalized,
        "evidence_text": {
            "selected": selected,
            "native": region.native_text or None,
            "ocr": visible_ocr or None,
            "token_agreement": agreement,
        },
    }
    structure = _text_structure(raw)
    if structure:
        content["structure"] = structure
    if block_type == "contact":
        content.update(_parse_contact_details(raw, normalized))
    return SourceBlock(
        document_id=document_id, type=block_type, page=region.page,
        block_id=f"{region.region_id}-block",
        content=content, coordinates=region.coordinates,
        extraction_method=methods, confidence=confidence,
        validation_status="passed" if normalized and not warnings else "needs_review", errors=errors, warnings=warnings,
        provenance=_provenance(source_hash, region, lines, image),
        semantic_role=_semantic_role_for_text(block_type, normalized, region.coordinates, region.page),
        heading_level=1 if block_type == "heading" and region.coordinates[1] <= 220 else (
            2 if block_type == "heading" else None
        ),
    )


def _visual_text_panel_blocks(
    document_id: str, source_hash: str, inspection: PageInspection, region: Region,
    lines: list[dict[str, Any]], image: Path, page_lines: list[dict[str, Any]],
) -> list[SourceBlock]:
    """Create a structural parent and leaf blocks for mixed text embedded in one image region."""
    groups = _split_visual_text_lines(lines)
    if len(groups) <= 1:
        region.native_text = ""
        return [_text_block(document_id, source_hash, inspection, region, lines, image, 1.1)]

    parent_id = f"{region.region_id}-content-group"
    children: list[SourceBlock] = []
    for index, group_lines in enumerate(groups, 1):
        coordinates = _line_box(group_lines)
        text = clean_text(_ocr_text(group_lines))
        subregion = Region(
            region_id=f"{region.region_id}-s{index:03d}", page=region.page,
            kind="normal_text", coordinates=coordinates, reading_order=region.reading_order,
            classification_method="visual-text-whitespace-segmentation",
            confidence=sum(float(line.get("confidence", 0.0)) for line in group_lines) / len(group_lines),
            metadata={
                "word_count": len(re.findall(r"\S+", text)),
                "median_font_size": statistics.median(
                    float(line["coordinates"][3]) * inspection.height_points / 1000.0 for line in group_lines
                ),
                "source_bbox_points": [
                    coordinates[0] * inspection.width_points / 1000.0,
                    coordinates[1] * inspection.height_points / 1000.0,
                    (coordinates[0] + coordinates[2]) * inspection.width_points / 1000.0,
                    (coordinates[1] + coordinates[3]) * inspection.height_points / 1000.0,
                ],
            },
        )
        child = _text_block(document_id, source_hash, inspection, subregion, group_lines, image, 1.1)
        child.parent_block_id = parent_id
        child.hierarchy_depth = 1
        child.heading_level = 2 if child.type == "heading" else None
        child.semantic_role = (
            "brand_mark_text" if _looks_like_brand_mark(group_lines, page_lines)
            else _semantic_role_for_text(child.type, text, coordinates, region.page, nested=True)
        )
        children.append(child)

    parent = SourceBlock(
        document_id=document_id, type="group", page=region.page, block_id=parent_id,
        content={"role": "mixed_text_panel", "child_block_ids": [child.block_id for child in children]},
        coordinates=region.coordinates,
        extraction_method=["RapidOCR", "PP-OCRv6", "Python whitespace segmentation"],
        confidence=min(child.confidence for child in children), validation_status="passed",
        provenance=_provenance(source_hash, region, [], image),
        semantic_role="content_panel", child_block_ids=[child.block_id for child in children],
    )
    return [parent, *children]


def _table_blocks(
    document_id: str, source_hash: str, region: Region, lines: list[dict[str, Any]], image: Path,
    qwen_payload: dict[str, Any] | None = None, qwen_error: str | None = None,
) -> list[SourceBlock]:
    rows = region.metadata.get("rows") or []
    table_title = region.metadata.get("title")
    row_sections = region.metadata.get("row_sections") or []
    cell_coordinates = region.metadata.get("cell_coordinates") or []
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
    if len(rows) == 1 and width >= 2:
        sections = []
        for index, value in enumerate(rows[0], 1):
            raw_text = clean_text(str(value or ""))
            first_line = raw_text.split("\n", 1)[0].strip() if raw_text else None
            sections.append({
                "section_id": f"s{index:03d}", "title": first_line,
                "text": raw_text, "structure_complete": False,
            })
        comparison_methods = ["native PDF layout"]
        if qwen_payload is not None:
            comparison_methods.append("Qwen3-VL table structure review")
        comparison_methods.append("Python section reconstruction")
        comparison_warnings = [
            "panel columns were preserved, but nested subsections could not be separated reliably"
        ]
        if qwen_error:
            comparison_warnings.append(f"Qwen semantic table stage unavailable: {qwen_error}")
        return [SourceBlock(
            document_id=document_id, type="comparison_panel", page=region.page,
            block_id=f"{region.region_id}-comparison-panel",
            content={"title": region.metadata.get("title"), "sections": sections},
            coordinates=region.coordinates,
            extraction_method=comparison_methods,
            confidence=min(region.confidence, 0.60), validation_status="needs_review",
            errors=[], warnings=comparison_warnings,
            provenance=_provenance(source_hash, region, lines, image),
        )]
    reconciliation = _table_total_reconciliation(rows)
    reconciliation_failed = bool(reconciliation and not reconciliation["passed"])
    parent_id = f"{region.region_id}-table"
    warnings = [] if native else ["table reconstructed from OCR geometry; PaddleOCR table structure was unavailable"]
    valid_shape = len(rows) >= 2 and width >= 2
    methods = ["native PDF table parser"] if native else ["RapidOCR", "PP-OCRv6"]
    if qwen_payload is not None:
        methods.append("Qwen3-VL table structure review")
    methods.append("Python table reconstruction and validation")
    headers = ["" if value is None else str(value).strip() for value in rows[0]] if rows else []
    column_kinds: list[str] = []
    for column_index, header in enumerate(headers):
        values = [str(row[column_index] or "") for row in rows[1:] if column_index < len(row)]
        joined = " ".join(values)
        if column_index == 0:
            kind_hint = "text"
        elif "%" in header or "%" in joined:
            kind_hint = "percent"
        elif "$" in joined or any(term in header.casefold() for term in {"invested", "capital", "debt", "equity", "interest"}):
            kind_hint = "currency"
        elif re.search(r"\bx\b", joined, re.I):
            kind_hint = "multiple"
        else:
            kind_hint = "number"
        column_kinds.append(kind_hint)
    columns = [
        {
            "column_id": f"c{index + 1:03d}", "label": label, "value_kind": column_kinds[index],
            "coordinates": cell_coordinates[0][index] if cell_coordinates and index < len(cell_coordinates[0]) else None,
            "evidence_ids": [f"{region.region_id}-native-table-r000-c{index:03d}"]
            if cell_coordinates and index < len(cell_coordinates[0]) and cell_coordinates[0][index] else [],
        }
        for index, label in enumerate(headers)
    ]
    typed_rows = []
    for row_index, row in enumerate(rows[1:], 1):
        label = "" if not row or row[0] is None else str(row[0]).strip()
        cells = []
        for column_index, value in enumerate(row[1:], 1):
            raw = None if value is None or str(value).strip() == "" else str(value).strip()
            state = "blank" if raw is None else "dash" if raw in {"-", "–", "—"} else "present"
            numeric_value, unit, normalized_value = _numeric_value(raw or "")
            if state != "present":
                numeric_value = unit = normalized_value = None
            elif column_index < len(column_kinds) and column_kinds[column_index] == "currency" and numeric_value is not None:
                unit = "USD"
                normalized_value = numeric_value
            cells.append({
                "column_id": f"c{column_index + 1:03d}", "raw_value": raw,
                "value_state": state, "numeric_value": numeric_value,
                "unit": unit, "normalized_value": normalized_value,
                "coordinates": (
                    cell_coordinates[row_index][column_index]
                    if row_index < len(cell_coordinates) and column_index < len(cell_coordinates[row_index])
                    else None
                ),
                "evidence_ids": [f"{region.region_id}-native-table-r{row_index:03d}-c{column_index:03d}"]
                if (
                    row_index < len(cell_coordinates) and column_index < len(cell_coordinates[row_index])
                    and cell_coordinates[row_index][column_index] and raw is not None
                ) else [],
            })
        typed_rows.append({
            "row_id": f"r{row_index:03d}",
            "section": row_sections[row_index] if row_index < len(row_sections) else None,
            "label": label, "cells": cells,
        })
    structure_errors = _table_structure_errors(headers, rows)
    qwen_warnings: list[str] = []
    review = qwen_payload.get("table_review") if qwen_payload else None
    if qwen_error:
        qwen_warnings.append(f"Qwen semantic table stage unavailable: {qwen_error}")
    elif qwen_payload is not None and not isinstance(review, dict):
        qwen_warnings.append("Qwen semantic table stage returned no structured table review")
    elif isinstance(review, dict):
        model_rows = review.get("data_row_count")
        model_columns = review.get("column_count")
        if model_rows != len(typed_rows) or model_columns != width or review.get("structure_matches") is not True:
            qwen_warnings.append(
                "Qwen table structure review disagreed with deterministic row/column reconstruction"
            )
    table_passed = valid_shape and native and not reconciliation_failed and not structure_errors
    parent = SourceBlock(
        document_id=document_id, type="table", page=region.page, block_id=parent_id,
        content={
            "title": table_title, "columns": columns, "rows": typed_rows,
            "row_count": len(typed_rows), "column_count": width,
            "reconciliation": reconciliation,
        },
        coordinates=region.coordinates, extraction_method=methods, confidence=region.confidence if native else 0.55,
        validation_status="passed" if table_passed and not qwen_warnings else "needs_review",
        errors=([] if valid_shape else ["table does not contain at least two rows and two columns"])
        + (["table subtotals do not reconcile with the final total"] if reconciliation_failed else [])
        + structure_errors, warnings=warnings + qwen_warnings,
        provenance=_provenance(source_hash, region, lines, image),
    )
    return [parent]


def _table_structure_errors(headers: list[str], rows: list[list[Any]]) -> list[str]:
    """Detect malformed ownership that numeric reconciliation cannot reveal."""
    errors: list[str] = []
    if any(not header for header in headers):
        errors.append("one or more table columns have no header owner")
    if any(header.count("(") != header.count(")") for header in headers):
        errors.append("one or more table headers contain unmatched parentheses")
    for row in rows[1:]:
        for value in row[1:]:
            text = str(value or "").strip()
            if text in {"$", "€", "£"}:
                errors.append("standalone currency symbol has no owned numeric value")
            if re.search(r"%\s*[$€£]$", text):
                errors.append("currency symbol is attached to a percentage cell")
    return sorted(set(errors))


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
    numeric_count = sum(bool(NUMBER.search(str(line.get("text", "")))) for line in lines)
    magnitude_count = sum(
        bool(re.fullmatch(r"(?:thousand|million|billion|k|m|mm|b)", str(line.get("text", "")).strip(), re.I))
        for line in lines
    )
    currency_count = sum(
        bool(re.fullmatch(r"[$€£]", str(line.get("text", "")).strip())) for line in lines
    )
    if image_count >= 1 and numeric_count >= 2 and magnitude_count >= 1 and currency_count >= 1:
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


def _map_data_value_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return map data values while excluding nearby color-scale endpoints."""
    percentage_lines = [
        line for line in lines
        if re.fullmatch(r"[-+]?\d[\d,.]*\s*%", str(line.get("text", "")).strip())
    ]
    legend_labels = [
        line for line in lines
        if "%" in str(line.get("text", ""))
        and not re.search(r"\d", str(line.get("text", "")))
    ]
    return [
        value for value in percentage_lines
        if not any(
            abs(_center(value["coordinates"])[1] - _center(label["coordinates"])[1]) <= 75
            for label in legend_labels
        )
    ]


def _qwen_semantic_prompt(
    kind: str, lines: list[dict[str, Any]], region: Region,
    target_value_ids: list[str] | None = None,
) -> str:
    """Build an evidence-aware semantic prompt after OCR and geometry stages complete."""
    evidence = [
        {
            "evidence_id": str(line.get("evidence_id", "")),
            "text": str(line.get("text", "")),
            "coordinates": [round(float(value), 2) for value in line.get("coordinates", [])],
        }
        for line in lines[:100]
        if str(line.get("text", "")).strip()
    ]
    if kind == "table":
        rows = region.metadata.get("rows") or []
        width = max((len(row) for row in rows), default=0)
        candidate = {
            "data_row_count": max(0, len(rows) - 1),
            "column_count": width,
            "headers": [str(value or "") for value in rows[0]] if rows else [],
        }
        return (
            "Verify only this table structure and return immediately as one JSON object with keys type, "
            "confidence, chart_type, bindings, table_review. Use type table, chart_type null, and bindings []. "
            "table_review must contain data_row_count (excluding the header), column_count, headers, and "
            "structure_matches. Do not transcribe cells or explain. Count section labels as labels, not data "
            "rows.\nCandidate:\n"
            + json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        )
    contract = (
        "Keep bindings compact: return only label, label_evidence_id, and value_evidence_id. Use only supplied "
        "evidence IDs. The pipeline will copy the visible OCR value from value_evidence_id. For charts, label "
        "must be the visible category and label_evidence_id is required. For maps, label must be the US state "
        "or territory owning the value and label_evidence_id may be null. Do not omit clear bindings or invent "
        "values."
    )
    target_instruction = ""
    if kind == "map" and target_value_ids:
        target_instruction = (
            " Return at most one binding for each of these target value IDs and no bindings for other values: "
            + json.dumps(target_value_ids, ensure_ascii=False, separators=(",", ":"))
            + ". Omit a target when it is a legend endpoint or its geography is not visually clear."
        )
    return (
        f"This is the mandatory semantic-linking stage after OCR and OpenCV geometry for a {kind}. "
        "Return exactly one JSON object with exactly four keys: type, confidence, chart_type, bindings. "
        f"type must be {kind}; confidence must be a number from zero to one. "
        + contract
        + target_instruction
        + "\nOCR evidence (normalized page coordinates):\n"
        + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    )


def _ground_model_bindings(
    kind: str, payload: dict[str, Any] | None, lines: list[dict[str, Any]],
    mark_boxes: list[list[float]],
) -> list[dict[str, Any]]:
    """Ground model associations back to exact OCR evidence before accepting them."""
    if not payload:
        return []
    by_id = {str(line.get("evidence_id")): line for line in lines}

    def resolve_line(evidence_id: Any, text: str, require_unique: bool = True) -> dict[str, Any] | None:
        candidate = by_id.get(str(evidence_id)) if evidence_id else None
        if candidate is not None:
            return candidate
        folded = _fold_token(text.strip())
        matches = [line for line in lines if _fold_token(str(line.get("text", "")).strip()) == folded]
        if len(matches) == 1 or (matches and not require_unique):
            return matches[0]
        return None

    grounded: list[dict[str, Any]] = []
    for binding in payload.get("bindings", []):
        label_text = str(
            binding.get("label") or binding.get("category") or binding.get("geography") or ""
        ).strip()
        value_text = str(binding.get("value") or binding.get("raw_value") or "").strip()
        value_id = binding.get("value_evidence_id")
        label_id = binding.get("label_evidence_id")
        if not label_text and not label_id:
            continue
        if not value_text and not value_id:
            continue
        value_line = resolve_line(value_id, value_text)
        label_line = resolve_line(label_id, label_text)
        if value_line is None or (kind == "chart" and label_line is None):
            continue
        if kind == "map" and label_text.casefold() not in US_GEOGRAPHIES:
            continue
        # OCR remains authoritative for the visible value and chart category.
        value_text = str(value_line.get("text", "")).strip()
        if label_line is not None and (kind == "chart" or not label_text):
            label_text = str(label_line.get("text", "")).strip()
        numeric_value, unit, normalized_value = _numeric_value(value_text)
        label_coordinates = label_line.get("coordinates") if label_line else None
        value_coordinates = value_line.get("coordinates")
        evidence_box = _box_union(*(
            box for box in (label_coordinates, value_coordinates) if box is not None
        ))
        grounded.append({
            "label": label_text,
            "value": value_text,
            "numeric_value": numeric_value,
            "unit": unit,
            "normalized_value": normalized_value,
            "label_evidence_id": label_line.get("evidence_id") if label_line else None,
            "value_evidence_id": value_line.get("evidence_id"),
            "label_coordinates": label_coordinates,
            "value_coordinates": value_coordinates,
            "visual_mark_coordinates": _nearest_mark(evidence_box, mark_boxes),
            "coordinates": evidence_box,
            "grounding_method": "Qwen semantic association grounded to OCR evidence and OpenCV geometry",
            "confidence": min(float(payload.get("confidence", 0.0)), float(binding.get("confidence", 1.0))),
        })
    # A model may repeat a value or assign it to multiple owners. Collapse
    # identical repeats, but reject the entire value when ownership conflicts.
    by_value: dict[str, list[dict[str, Any]]] = {}
    for binding in grounded:
        by_value.setdefault(str(binding.get("value_evidence_id") or ""), []).append(binding)
    unambiguous: list[dict[str, Any]] = []
    for candidates in by_value.values():
        labels = {_fold_token(str(candidate.get("label", ""))) for candidate in candidates}
        if len(labels) != 1:
            continue
        unambiguous.append(max(candidates, key=lambda candidate: float(candidate.get("confidence", 0.0))))
    return unambiguous


def _reconcile_visual_bindings(
    deterministic: list[dict[str, Any]], model: list[dict[str, Any]], allow_model_additions: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Union grounded model evidence with deterministic evidence without allowing replacement."""
    reconciled = list(deterministic)
    warnings: list[str] = []
    by_value_id = {
        str(binding.get("value_evidence_id")): binding
        for binding in reconciled if binding.get("value_evidence_id")
    }
    confirmed = 0
    conflicts = 0
    added = 0
    unverified = 0
    for binding in model:
        value_id = str(binding.get("value_evidence_id") or "")
        existing = by_value_id.get(value_id)
        if existing is None:
            if not allow_model_additions:
                unverified += 1
                continue
            reconciled.append(binding)
            if value_id:
                by_value_id[value_id] = binding
            added += 1
            continue
        if _fold_token(str(existing.get("label", ""))) == _fold_token(str(binding.get("label", ""))):
            existing["grounding_method"] = f"{existing.get('grounding_method')}; Qwen-confirmed"
            existing["confidence"] = max(float(existing.get("confidence", 0.0)), float(binding.get("confidence", 0.0)))
            confirmed += 1
        else:
            conflicts += 1
    if conflicts:
        warnings.append(f"Qwen disagreed with deterministic ownership for {conflicts} value(s); deterministic evidence retained")
    if model and len(model) < len(deterministic):
        warnings.append(
            f"Qwen returned {len(model)} grounded binding(s) for {len(deterministic)} deterministic binding(s); "
            "deterministic evidence retained"
        )
    if model:
        warnings.append(f"Qwen semantic reconciliation: {confirmed} confirmed, {added} added, {conflicts} conflicted")
    if unverified:
        warnings.append(
            f"Qwen proposed {unverified} semantic ownership binding(s) that Python could not independently validate; omitted"
        )
    return reconciled, warnings


def _pie_label_value_grounding(
    bindings: list[dict[str, Any]], masked_slices: list[dict[str, Any]] | None = None,
) -> bool:
    """Bind pie values to distinct PDF alpha masks only when mask areas reconcile."""
    candidates = list(masked_slices or [])
    verified = False
    if 3 <= len(bindings) == len(candidates) <= 12 and all(
        binding.get("unit") == "percent" and binding.get("numeric_value") is not None
        for binding in bindings
    ):
        total_area = sum(float(candidate.get("projected_alpha_area") or 0) for candidate in candidates)
        if total_area > 0 and len({candidate.get("smask_sha256") for candidate in candidates}) == len(candidates):
            areas = [100 * float(candidate["projected_alpha_area"]) / total_area for candidate in candidates]

            @lru_cache(maxsize=None)
            def assignment(index: int, used: int) -> tuple[float, tuple[int, ...]]:
                if index == len(bindings):
                    return 0.0, ()
                binding = bindings[index]
                evidence_box = _box_union(
                    binding.get("label_coordinates"), binding.get("value_coordinates"),
                )
                owner_center = _center(evidence_box)
                target = float(binding["numeric_value"])
                best: tuple[float, tuple[int, ...]] = (float("inf"), ())
                for candidate_index, candidate in enumerate(candidates):
                    if used & (1 << candidate_index):
                        continue
                    centroid = candidate["alpha_centroid_coordinates"]
                    spatial_cost = math.dist(owner_center, centroid) / 1000.0
                    area_cost = abs(areas[candidate_index] - target) * 20.0
                    remaining, tail = assignment(index + 1, used | (1 << candidate_index))
                    trial = (area_cost + spatial_cost + remaining, (candidate_index,) + tail)
                    if trial[0] < best[0]:
                        best = trial
                return best

            _, owners = assignment(0, 0)
            verified = len(owners) == len(bindings) and all(
                abs(areas[candidate_index] - float(binding["numeric_value"]))
                <= max(0.15, float(binding["numeric_value"]) * 0.02)
                for binding, candidate_index in zip(bindings, owners)
            )
            if verified:
                for binding, candidate_index in zip(bindings, owners):
                    candidate = candidates[candidate_index]
                    qwen_confirmed = "Qwen-confirmed" in str(binding.get("grounding_method") or "")
                    binding["visual_mark_coordinates"] = candidate["mark_coordinates"]
                    binding["visual_mark_ref"] = {
                        "source": "pdf_soft_mask",
                        "pdf_image_index": candidate["pdf_image_index"],
                        "smask_sha256": candidate["smask_sha256"],
                        "opacity_weighted_area_share_percent": round(areas[candidate_index], 5),
                    }
                    binding["grounding_method"] = (
                        "OCR label-value spatial association"
                        + ("; Qwen-confirmed label-value association" if qwen_confirmed else "")
                        + "; one-to-one PDF soft-mask opacity-weighted area/value and position match"
                    )
    if verified:
        return True
    for binding in bindings:
        qwen_confirmed = "Qwen-confirmed" in str(binding.get("grounding_method") or "")
        binding["visual_mark_coordinates"] = None
        binding["visual_mark_ref"] = None
        binding["grounding_method"] = (
            "OCR label-value spatial association"
            + ("; Qwen-confirmed label-value association" if qwen_confirmed else "")
            + "; individual pie-slice geometry unresolved"
        )
    return False


def _dedupe_spatial_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer longer OCR phrases while removing equivalent native/OCR duplicates."""
    kept: list[dict[str, Any]] = []
    for line in sorted(
        lines,
        key=lambda item: (
            0 if "-ocr-" in str(item.get("evidence_id", "")) else 1,
            -len(str(item.get("text", ""))),
        ),
    ):
        text = _fold_token(str(line.get("text", "")).strip())
        if not text:
            continue
        cx, cy = _center(line["coordinates"])
        if any(
            _fold_token(str(existing.get("text", "")).strip()) == text
            and abs(cx - _center(existing["coordinates"])[0]) <= 20
            and abs(cy - _center(existing["coordinates"])[1]) <= 35
            for existing in kept
        ):
            continue
        kept.append(line)
    return kept


def _as_of_date(lines: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for line in lines:
        text = str(line.get("text", "")).strip()
        match = re.search(r"\b(?:as\s+of\s+)?(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text, re.I)
        if not match:
            continue
        month, day, year = (int(value) for value in match.groups())
        year += 2000 if year < 100 else 0
        try:
            return datetime(year, month, day).date().isoformat(), str(line.get("evidence_id"))
        except ValueError:
            return None, str(line.get("evidence_id"))
    return None, None


def _join_kpi_label(lines: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
    if not lines:
        return None, None
    ordered = sorted(lines, key=lambda line: (line["coordinates"][1], line["coordinates"][0]))
    parts: list[str] = []
    used: list[dict[str, Any]] = []
    accumulated = ""
    for line in ordered:
        text = re.sub(r"\s+", " ", str(line.get("text", ""))).strip(" -")
        if not text:
            continue
        folded = _fold_token(text)
        if folded in _fold_token(accumulated):
            continue
        # Native positioned words frequently duplicate a complete OCR phrase.
        if any(folded in _fold_token(str(other.get("text", ""))) and other is not line for other in ordered):
            continue
        parts.append(text)
        used.append(line)
        accumulated = " ".join(parts)
    if not parts:
        return None, None
    return " ".join(parts), used[0]


def _kpi_bindings(
    lines: list[dict[str, Any]], mark_boxes: list[list[float]],
) -> tuple[list[dict[str, Any]], str | None, int]:
    """Reconstruct repeated amount/count KPI cards from geometry and unit ownership."""
    lines = _dedupe_spatial_lines(lines)
    as_of, _ = _as_of_date(lines)
    numeric_lines = [
        line for line in lines
        if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", str(line.get("text", "")).strip())
    ]
    unit_lines = [
        line for line in lines
        if re.fullmatch(r"(?:thousand|million|billion|k|m|mm|b)", str(line.get("text", "")).strip(), re.I)
    ]
    currency_lines = [
        line for line in lines if re.fullmatch(r"[$€£]", str(line.get("text", "")).strip())
    ]
    amount_rows: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    used_numbers: set[str] = set()
    for unit in sorted(unit_lines, key=lambda item: item["coordinates"][1]):
        ux, uy = _center(unit["coordinates"])
        candidates = []
        for number in numeric_lines:
            nx, ny = _center(number["coordinates"])
            unit_top = unit["coordinates"][1] - 25
            unit_bottom = unit["coordinates"][1] + unit["coordinates"][3] + 25
            if number["evidence_id"] not in used_numbers and nx < ux + 25 and unit_top <= ny <= unit_bottom:
                candidates.append(number)
        if not candidates:
            continue
        number = min(candidates, key=lambda item: _line_distance(item, unit))
        nx, ny = _center(number["coordinates"])
        currency = min(
            (
                item for item in currency_lines
                if _center(item["coordinates"])[0] < nx
                and abs(_center(item["coordinates"])[1] - ny) <= 90
            ),
            key=lambda item: _line_distance(item, number),
            default=None,
        )
        if currency is None:
            continue
        used_numbers.add(str(number["evidence_id"]))
        amount_rows.append((number, unit, currency))
    amount_rows.sort(key=lambda row: _center(row[0]["coordinates"])[1])
    if not amount_rows:
        return [], as_of, 0

    bindings: list[dict[str, Any]] = []
    completed_groups = 0
    centers = [_center(row[0]["coordinates"])[1] for row in amount_rows]
    for index, (amount, unit, currency) in enumerate(amount_rows):
        amount_x, amount_y = _center(amount["coordinates"])
        lower = (centers[index - 1] + amount_y) / 2 if index else amount_y - 140
        upper = (amount_y + centers[index + 1]) / 2 if index + 1 < len(centers) else amount_y + 180
        count_candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for number in numeric_lines:
            if number["evidence_id"] in used_numbers:
                continue
            nx, ny = _center(number["coordinates"])
            if not lower <= ny <= upper:
                continue
            owners = [
                line for line in lines
                if re.search(r"[A-Za-z]", str(line.get("text", "")))
                and float(line["coordinates"][2]) >= 1.2 * max(1.0, float(line["coordinates"][3]))
                and not NUMBER.search(str(line.get("text", "")))
                and not re.search(r"\bas\s+of\b", str(line.get("text", "")), re.I)
                and not re.fullmatch(
                    r"(?:thousand|million|billion|k|m|mm|b)",
                    str(line.get("text", "")).strip(), re.I,
                )
                and 1 <= len(str(line.get("text", "")).split()) <= 4
                and abs(_center(line["coordinates"])[0] - nx) <= 130
                and 0 <= _center(line["coordinates"])[1] - ny <= 100
            ]
            if owners:
                owner = min(owners, key=lambda line: _line_distance(number, line))
                count_candidates.append((_line_distance(number, owner), number, owner))
        count = count_owner = None
        if count_candidates:
            _, count, count_owner = min(count_candidates, key=lambda item: item[0])

        count_x = _center(count["coordinates"])[0] if count else 1000.0
        label_lines = [
            line for line in lines
            if "-ocr-" in str(line.get("evidence_id", ""))
            and re.search(r"[A-Za-z]", str(line.get("text", "")))
            and not re.search(r"\bas\s+of\b", str(line.get("text", "")), re.I)
            and not re.fullmatch(r"(?:thousand|million|billion|k|m|mm|b)", str(line.get("text", "")).strip(), re.I)
            and lower <= _center(line["coordinates"])[1] <= upper
            and _center(line["coordinates"])[1] >= amount_y + 45
            and abs(_center(line["coordinates"])[0] - amount_x) <= 190
            and _center(line["coordinates"])[0] < count_x - 30
        ]
        label, label_line = _join_kpi_label(label_lines)
        if not label or not label_line:
            continue
        raw_amount = f"{str(currency['text']).strip()}{str(amount['text']).strip()} {str(unit['text']).strip().lower()}"
        numeric_value = float(str(amount["text"]).replace(",", ""))
        multiplier = {
            "thousand": 1_000.0, "k": 1_000.0,
            "million": 1_000_000.0, "m": 1_000_000.0, "mm": 1_000_000.0,
            "billion": 1_000_000_000.0, "b": 1_000_000_000.0,
        }[_fold_token(str(unit["text"]).strip())]
        amount_mark = _nearest_mark(_box_union(amount["coordinates"], unit["coordinates"]), mark_boxes)
        bindings.append({
            "label": "amount", "series": label, "value": raw_amount,
            "numeric_value": numeric_value, "unit": "USD", "normalized_value": numeric_value * multiplier,
            "label_evidence_id": label_line["evidence_id"], "value_evidence_id": amount["evidence_id"],
            "label_coordinates": _box_union(*(line["coordinates"] for line in label_lines)),
            "value_coordinates": _box_union(currency["coordinates"], amount["coordinates"], unit["coordinates"]),
            "visual_mark_coordinates": amount_mark,
            "grounding_method": "currency-number-unit row with vertically owned KPI label",
            "confidence": 0.94,
        })
        if count is not None and count_owner is not None:
            count_value = float(str(count["text"]).replace(",", ""))
            bindings.append({
                "label": str(count_owner["text"]).strip(), "series": label, "value": str(count["text"]).strip(),
                "numeric_value": count_value, "unit": "count", "normalized_value": count_value,
                "label_evidence_id": count_owner["evidence_id"], "value_evidence_id": count["evidence_id"],
                "label_coordinates": count_owner["coordinates"], "value_coordinates": count["coordinates"],
                "visual_mark_coordinates": _nearest_mark(_box_union(count["coordinates"], count_owner["coordinates"]), mark_boxes),
                "grounding_method": "number with vertically adjacent count label inside KPI row",
                "confidence": 0.94,
            })
            completed_groups += 1
    return bindings, as_of, completed_groups


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
    min_mark_x = min(mark[0] for mark in marks)
    max_mark_x = max(mark[0] + mark[2] for mark in marks)
    series_labels = sorted([
        line for line in lines
        if _center(line["coordinates"])[1] > baseline
        and min_mark_x - 120 <= _center(line["coordinates"])[0] <= max_mark_x + 120
        and 1 <= len(str(line["text"]).split()) <= 8
        and not NUMBER.search(str(line["text"]))
        and str(line["text"]).strip() != (title or "")
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
        methods.insert(-1, "Qwen3-VL semantic linking")
    labels = [str(line["text"]) for line in lines]
    title = _visual_title(kind, lines)
    chart_type = _infer_chart_type(region, lines, features, qwen_payload) if kind == "chart" else None
    if kind == "chart" and chart_type == "kpi_panel":
        kind = "kpi_panel"
    parent_status = "passed" if classification_confidence >= 0.75 else "needs_review"
    if kind == "unclassified_visual":
        parent_status = "needs_review"
        classification_warnings = list(classification_warnings) + [
            "unclassified visual cannot be accepted as a semantic block"
        ]
    if kind == "decoration" and int(region.metadata.get("semantic_text_overlap_count", 0)):
        parent_status = "needs_review"
        classification_warnings = list(classification_warnings) + [
            "decorative image overlaps semantic text; retain as background evidence only"
        ]
    base_visual = {
        "vision_summary": _vision_summary(features),
        "vision_features_ref": vision_features_ref,
        "region_image": f"region-images/{crop_path.name}",
    }
    if kind == "decoration":
        content: dict[str, Any] = {
            "role": "repeated_page_band" if region.metadata.get("repeated_on_pages") else "decorative_artwork",
            "repeated_on_pages": region.metadata.get("repeated_on_pages"),
            **base_visual,
        }
    elif kind == "photograph":
        content = {"caption": title, **base_visual}
    elif kind == "brand_mark":
        content = {"visible_text": labels, "evidence_mode": "visual_region", **base_visual}
    elif kind == "unclassified_visual":
        content = {"visible_text": labels, **base_visual}
    elif kind == "kpi_panel":
        content = {"title": None, "as_of": None, "metrics": [], **base_visual}
    elif kind == "map":
        content = {"title": title, "bindings": [], **base_visual}
    else:
        content = {"title": title, "chart_type": chart_type, "observations": [], **base_visual}
    parent = SourceBlock(
        document_id=document_id, type=kind, page=region.page, block_id=parent_id,
        content=content,
        coordinates=region.coordinates, extraction_method=methods, confidence=classification_confidence,
        validation_status=parent_status,
        errors=[] if labels or kind in {"unclassified_visual", "photograph", "decoration"} else ["no visible labels recovered"],
        warnings=classification_warnings,
        provenance=_provenance(source_hash, region, lines, image),
        semantic_role="background_decoration" if kind == "decoration" else None,
    )
    if kind not in {"chart", "map", "kpi_panel"}:
        return [parent]
    mark_boxes = region.metadata.get("member_coordinates", [])
    deterministic_bindings: list[dict[str, Any]] = []
    completed_kpi_groups = 0
    if chart_type == "bar":
        deterministic_bindings = _bar_bindings(lines, title, mark_boxes)
    elif chart_type == "scatterplot":
        parent.warnings.append("scatterplot points require axis-calibrated reconstruction")
    elif chart_type == "kpi_panel" or kind == "kpi_panel":
        deterministic_bindings, as_of, completed_kpi_groups = _kpi_bindings(lines, mark_boxes)
        parent.content["as_of"] = as_of
    else:
        deterministic_bindings = _proximity_bindings(lines, title, mark_boxes)

    if kind == "map":
        rejected = [
            binding for binding in deterministic_bindings
            if str(binding.get("label") or binding.get("geography") or "").strip().casefold() not in US_GEOGRAPHIES
        ]
        deterministic_bindings = [binding for binding in deterministic_bindings if binding not in rejected]
        if rejected:
            parent.validation_status = "needs_review"
            parent.warnings.append(
                f"discarded {len(rejected)} label-value pairs without a recognized geography owner"
            )
    model_bindings = _ground_model_bindings(kind, qwen_payload, lines, mark_boxes)
    bindings, reconciliation_warnings = _reconcile_visual_bindings(
        deterministic_bindings, model_bindings, allow_model_additions=False,
    )
    parent.warnings.extend(reconciliation_warnings)
    if kind == "chart" and chart_type == "pie":
        slice_verified = _pie_label_value_grounding(
            bindings, region.metadata.get("pdf_soft_mask_slices", []),
        )
        parent.content["slice_geometry_status"] = "verified" if slice_verified else "unresolved"
        if not slice_verified:
            parent.validation_status = "needs_review"
            parent.warnings.append(
                "individual pie-slice geometry was not validated; label-value associations are retained"
            )
    if qwen_payload is not None and not model_bindings:
        parent.validation_status = "needs_review"
        parent.warnings.append("Qwen semantic stage returned no evidence-grounded bindings")
    qwen_semantic_failed = any(
        warning.startswith("Qwen semantic stage") for warning in parent.warnings
    )
    if qwen_semantic_failed:
        parent.validation_status = "needs_review"

    if kind == "chart" and chart_type == "pie":
        percentage_total = sum(
            float(binding["numeric_value"])
            for binding in bindings
            if binding.get("unit") == "percent" and binding.get("numeric_value") is not None
        )
        parent.content["percentage_total"] = round(percentage_total, 5)
        visible_percentage_count = sum(
            bool(re.fullmatch(r"[-+]?\d[\d,.]*\s*%", str(line.get("text", "")).strip()))
            for line in lines
        )
        parent.content["expected_observation_count"] = visible_percentage_count
        parent.content["emitted_observation_count"] = len(bindings)
        parent.content["observation_completeness"] = round(
            min(1.0, len(bindings) / visible_percentage_count), 5,
        ) if visible_percentage_count else 0.0
        parent.content["percentage_total_reconciles"] = (
            98.0 <= percentage_total <= 102.0
            and len(bindings) == visible_percentage_count
        )
        if len(bindings) >= 3 and parent.content["percentage_total_reconciles"] and not qwen_semantic_failed:
            parent.confidence = max(parent.confidence, 0.90)
            if slice_verified:
                parent.validation_status = "passed"
        else:
            parent.validation_status = "needs_review"
            parent.warnings.append("pie observations do not reconcile to a complete 100% allocation")
    if kind == "map":
        expected_values = _map_data_value_lines(lines)
        parent.content["expected_binding_count"] = len(expected_values)
        parent.content["emitted_binding_count"] = len(bindings)
        parent.content["binding_completeness"] = round(
            len(bindings) / len(expected_values), 5,
        ) if expected_values else 0.0
        if len(bindings) != len(expected_values):
            parent.validation_status = "needs_review"
            parent.warnings.append(
                f"validated {len(bindings)} of {len(expected_values)} visible map values"
            )
    expected_marks = region.metadata.get("expected_mark_count")
    if kind == "chart" and chart_type != "pie" and isinstance(expected_marks, int) and expected_marks > 0:
        parent.content["expected_observation_count"] = expected_marks
        parent.content["emitted_observation_count"] = len(bindings)
        parent.content["observation_completeness"] = round(min(1.0, len(bindings) / expected_marks), 5)
        if len(bindings) != expected_marks:
            parent.validation_status = "needs_review"
            parent.warnings.append(
                f"reconstructed {len(bindings)} of {expected_marks} detected visual marks"
            )
    nested_items: list[dict[str, Any]] = []
    for index, binding in enumerate(bindings, 1):
        label = str(binding.get("label") or binding.get("category") or binding.get("geography") or "").strip()
        value = str(binding.get("value") or "").strip()
        errors = []
        if not label:
            errors.append("binding has no owner label")
        if not value:
            errors.append("binding has no value")
        item_content = {
            "item_id": f"o{index:03d}" if kind in {"chart", "kpi_panel"} else f"b{index:03d}",
            "raw_value": value,
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
        if kind == "chart" and chart_type == "pie":
            item_content["visual_mark_ref"] = binding.get("visual_mark_ref")
        if kind in {"chart", "kpi_panel"}:
            item_content.update({"series": binding.get("series"), "category": label})
        else:
            item_content["geography"] = label
        selected_lines = [
            line for line in lines
            if line["evidence_id"] in {binding.get("label_evidence_id"), binding.get("value_evidence_id")}
        ]
        grounded = bool(selected_lines and binding.get("visual_mark_coordinates"))
        if kind == "chart" and chart_type == "pie":
            grounded = bool(
                binding.get("label_evidence_id") and binding.get("value_evidence_id")
                and len(selected_lines) == 2
            )
        confidence = float(binding.get("confidence", 0.65 if grounded else 0.48))
        item_content["confidence"] = round(confidence, 5)
        item_content["validation_status"] = "passed" if grounded and not errors and confidence >= 0.70 else "needs_review"
        item_content["errors"] = errors
        nested_items.append(item_content)
    if kind == "chart":
        parent.content["observations"] = nested_items
    elif kind == "map":
        parent.content["bindings"] = nested_items
    else:
        parent.content["metrics"] = nested_items
        if completed_kpi_groups >= 1 and len(bindings) == completed_kpi_groups * 2 and not qwen_semantic_failed:
            parent.validation_status = "passed"
            parent.confidence = max(parent.confidence, 0.90)
    if not bindings:
        parent.validation_status = "needs_review"
        parent.warnings.append("no owned visual observations reconstructed")
    return [parent]


def _attach_background_decorations(blocks: list[SourceBlock]) -> None:
    """Attach overlapping background artwork to the smallest structural group that contains it."""
    groups = [block for block in blocks if block.type == "group"]
    for decoration in (block for block in blocks if block.type == "decoration"):
        dx, dy, dw, dh = decoration.coordinates
        center = (dx + dw / 2.0, dy + dh / 2.0)
        containers = [
            group for group in groups
            if group.coordinates[0] <= center[0] <= group.coordinates[0] + group.coordinates[2]
            and group.coordinates[1] <= center[1] <= group.coordinates[1] + group.coordinates[3]
        ]
        if not containers:
            continue
        parent = min(containers, key=lambda group: group.coordinates[2] * group.coordinates[3])
        decoration.parent_block_id = parent.block_id
        decoration.hierarchy_depth = parent.hierarchy_depth + 1
        if decoration.block_id not in parent.child_block_ids:
            parent.child_block_ids.append(decoration.block_id)
        content_children = parent.content.setdefault("child_block_ids", [])
        if decoration.block_id not in content_children:
            content_children.append(decoration.block_id)


def _recover_unassigned_brand_marks(
    document_id: str, source_hash: str, inspection: PageInspection, image: Path,
    page_lines: list[dict[str, Any]], assigned: set[str], crops: Path,
    document_token_pages: Counter[str] | None = None,
) -> list[SourceBlock]:
    """Recover short logo text using page, document, and corner evidence."""
    candidates = [
        line for line in page_lines
        if line.get("evidence_id") not in assigned and float(line.get("confidence", 0.0)) >= 0.80
    ]
    if not candidates:
        return []
    groups: list[list[dict[str, Any]]] = []
    for line in sorted(candidates, key=lambda item: (item["coordinates"][1], item["coordinates"][0])):
        lx, ly, lw, lh = (float(value) for value in line["coordinates"])
        match = None
        for group in groups:
            gx, gy, gw, gh = _line_box(group)
            horizontal_overlap = max(0.0, min(lx + lw, gx + gw) - max(lx, gx))
            horizontal_related = horizontal_overlap > 0 or abs((lx + lw / 2) - (gx + gw / 2)) <= max(lw, gw)
            vertical_gap = max(0.0, ly - (gy + gh), gy - (ly + lh))
            if horizontal_related and vertical_gap <= max(35.0, 1.5 * max(lh, gh / len(group))):
                match = group
                break
        if match is None:
            groups.append([line])
        else:
            match.append(line)

    recovered: list[SourceBlock] = []
    for index, group in enumerate(groups, 1):
        if not _looks_like_brand_mark(group, page_lines, document_token_pages):
            continue
        coordinates = _line_box(group)
        region = Region(
            region_id=f"p{inspection.page:03d}-u{index:03d}", page=inspection.page,
            kind="brand_mark", coordinates=coordinates, reading_order=len(inspection.regions) + index,
            classification_method="unassigned-ocr-brand-evidence-recovery",
            confidence=sum(float(line.get("confidence", 0.0)) for line in group) / len(group),
            metadata={
                "source_bbox_points": [
                    coordinates[0] * inspection.width_points / 1000.0,
                    coordinates[1] * inspection.height_points / 1000.0,
                    (coordinates[0] + coordinates[2]) * inspection.width_points / 1000.0,
                    (coordinates[1] + coordinates[3]) * inspection.height_points / 1000.0,
                ],
            },
        )
        crop_path = crops / f"{region.region_id}.png"
        _crop(image, coordinates, crop_path)
        recovered.append(SourceBlock(
            document_id=document_id, type="brand_mark", page=inspection.page,
            block_id=f"{region.region_id}-brand-mark",
            content={
                "visible_text": _brand_visible_text(group, document_token_pages),
                "evidence_mode": "ocr_recovery",
                "vision_summary": None,
                "vision_features_ref": None,
                "region_image": f"region-images/{crop_path.name}",
            },
            coordinates=coordinates,
            extraction_method=["RapidOCR", "PP-OCRv6", "document-level brand recovery"],
            confidence=region.confidence, validation_status="passed",
            provenance=_provenance(source_hash, region, group, image), semantic_role="brand_mark",
        ))
        assigned.update(str(line["evidence_id"]) for line in group)
    return recovered


def _brand_visible_text(
    lines: list[dict[str, Any]], document_token_pages: Counter[str] | None,
) -> list[str]:
    """Split concatenated logo words only when document vocabulary supports the split."""
    vocabulary = {
        token for token, count in (document_token_pages or {}).items()
        if count >= 2 and len(token) >= 3
    }

    def segment(token: str) -> list[str] | None:
        folded = _fold_token(token)
        choices: list[list[str] | None] = [None] * (len(folded) + 1)
        choices[0] = []
        for end in range(1, len(folded) + 1):
            candidates: list[list[str]] = []
            for start in range(end):
                if start == 0 and end == len(folded):
                    continue
                if choices[start] is not None and folded[start:end] in vocabulary:
                    candidates.append([*choices[start], token[start:end]])
            if candidates:
                choices[end] = max(candidates, key=len)
        return choices[-1] if choices[-1] and len(choices[-1]) >= 2 else None

    visible = []
    for line in lines:
        words = str(line.get("text", "")).split()
        repaired = []
        for word in words:
            for piece in segment(word) or [word]:
                folded_piece = _fold_token(piece)
                if folded_piece in vocabulary and piece.isupper():
                    repaired.append(folded_piece.upper())
                else:
                    repaired.append(piece)
        visible.append(" ".join(repaired))
    return visible


def _document_token_page_frequency(
    ocr_by_page: dict[int, dict[str, Any]], document_name: str = "",
) -> Counter[str]:
    """Count on how many pages each alphabetic token appears."""
    frequency: Counter[str] = Counter()
    for page in ocr_by_page.values():
        tokens = {
            _fold_token(token)
            for line in page.get("lines", [])
            for token in re.findall(r"[^\W\d_]{2,}", str(line.get("text", "")), re.UNICODE)
            if _fold_token(token)
        }
        frequency.update(tokens)
    # Filename words are useful weak evidence for logos, but requiring a visual
    # corner signature still prevents ordinary filename terms becoming brands.
    for token in re.findall(r"[A-Za-z]{3,}", document_name):
        frequency[_fold_token(token)] += 2
    return frequency


def _cluster_spatial_lines(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Cluster nearby OCR lines into independently readable footer/header elements."""
    groups: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=lambda item: (float(item["coordinates"][0]), float(item["coordinates"][1]))):
        lx, ly, lw, lh = (float(value) for value in line["coordinates"])
        match = None
        for group in groups:
            gx, gy, gw, gh = _line_box(group)
            horizontal_overlap = max(0.0, min(lx + lw, gx + gw) - max(lx, gx))
            centers_close = abs((lx + lw / 2) - (gx + gw / 2)) <= max(lw, gw) * 0.75
            vertical_gap = max(0.0, ly - (gy + gh), gy - (ly + lh))
            if (horizontal_overlap > 0 or centers_close) and vertical_gap <= max(40.0, 1.5 * max(lh, gh / len(group))):
                match = group
                break
        if match is None:
            groups.append([line])
        else:
            match.append(line)
    return groups


def _native_words_for_box(region: Region, coordinates: list[float]) -> str:
    """Return positioned native words whose centers fall inside a normalized box."""
    x, y, width, height = coordinates
    selected = []
    for word in region.metadata.get("native_visual_words", []):
        wx, wy, ww, wh = (float(value) for value in word.get("coordinates", [0, 0, 0, 0]))
        center_x, center_y = wx + ww / 2, wy + wh / 2
        if x <= center_x <= x + width and y <= center_y <= y + height:
            selected.append(word)
    return " ".join(
        str(word.get("text", ""))
        for word in sorted(selected, key=lambda item: (float(item["coordinates"][1]), float(item["coordinates"][0])))
    ).strip()


def _semantic_page_band_blocks(
    document_id: str, source_hash: str, inspection: PageInspection, region: Region,
    lines: list[dict[str, Any]], page_lines: list[dict[str, Any]], image: Path,
    crops: Path, features: dict[str, Any], vision_features_ref: dict[str, str],
    native_threshold: float, document_token_pages: Counter[str] | None,
) -> list[SourceBlock]:
    """Decompose a repeated text-bearing page band instead of discarding it as decoration."""
    if not lines or int(region.metadata.get("semantic_text_overlap_count", 0)) <= 0:
        return []
    clusters = _cluster_spatial_lines(lines)
    if not clusters:
        return []
    parent_id = f"{region.region_id}-page-band"
    children: list[SourceBlock] = []
    for index, cluster in enumerate(clusters, 1):
        coordinates = _line_box(cluster)
        child_region = Region(
            region_id=f"{region.region_id}-band-{index:03d}", page=region.page,
            kind="normal_text", coordinates=coordinates,
            reading_order=region.reading_order + index,
            classification_method="semantic repeated-page-band decomposition",
            confidence=sum(float(line.get("confidence", 0.0)) for line in cluster) / len(cluster),
            native_text=_native_words_for_box(region, coordinates),
            metadata={"source_bbox_points": region.metadata.get("source_bbox_points")},
        )
        if _looks_like_brand_mark(cluster, page_lines, document_token_pages):
            crop_path = crops / f"{child_region.region_id}.png"
            _crop(image, coordinates, crop_path)
            child = SourceBlock(
                document_id=document_id, type="brand_mark", page=region.page,
                block_id=f"{child_region.region_id}-brand-mark",
                content={
                    "visible_text": _brand_visible_text(cluster, document_token_pages),
                    "evidence_mode": "visual_region",
                    "vision_summary": _vision_summary(features),
                    "vision_features_ref": vision_features_ref,
                    "region_image": f"region-images/{crop_path.name}",
                },
                coordinates=coordinates,
                extraction_method=["RapidOCR", "PP-OCRv6", "Python repeated-band decomposition"],
                confidence=max(0.82, child_region.confidence), validation_status="passed",
                # The shared diagnostic describes the parent visual band, so its
                # provenance region must match that diagnostic's region identity.
                provenance=_provenance(source_hash, region, cluster, image),
                semantic_role="brand_mark",
            )
        else:
            child = _text_block(
                document_id, source_hash, inspection, child_region, cluster, image, native_threshold,
            )
            child.semantic_role = "running_footer" if coordinates[1] >= 500 else "running_header"
        child.parent_block_id = parent_id
        child.hierarchy_depth = 1
        children.append(child)
    if not children:
        return []
    parent = SourceBlock(
        document_id=document_id, type="group", page=region.page, block_id=parent_id,
        content={"role": "page_footer" if region.coordinates[1] >= 500 else "page_header",
                 "child_block_ids": [child.block_id for child in children]},
        coordinates=region.coordinates,
        extraction_method=["Python semantic repeated-page-band decomposition"],
        confidence=min(child.confidence for child in children),
        validation_status="passed" if all(child.validation_status == "passed" for child in children) else "needs_review",
        warnings=[] if all(child.validation_status == "passed" for child in children) else ["one or more child blocks require review"],
        provenance=_provenance(source_hash, region, [], image),
        semantic_role="page_footer" if region.coordinates[1] >= 500 else "page_header",
        child_block_ids=[child.block_id for child in children],
    )
    return [parent, *children]


def _group_profile_rows(
    document_id: str, source_hash: str, inspection: PageInspection, image: Path,
    blocks: list[SourceBlock],
) -> None:
    """Pair aligned photographs with adjacent structured biography blocks."""
    photographs = [
        block for block in blocks
        if block.type == "photograph" and block.parent_block_id is None
    ]
    biographies = [
        block for block in blocks
        if block.type == "group" and block.parent_block_id is None
        and block.content.get("role") == "profile_biography"
    ]
    used_biographies: set[str] = set()
    for index, photograph in enumerate(sorted(photographs, key=lambda block: block.coordinates[1]), 1):
        px, py, pw, ph = photograph.coordinates
        candidates: list[tuple[float, SourceBlock]] = []
        for biography in biographies:
            if biography.block_id in used_biographies:
                continue
            bx, by, bw, bh = biography.coordinates
            overlap = max(0.0, min(py + ph, by + bh) - max(py, by))
            overlap_ratio = overlap / max(1.0, min(ph, bh))
            horizontal_gap = bx - (px + pw)
            if overlap_ratio >= 0.45 and -20 <= horizontal_gap <= 250:
                candidates.append((overlap_ratio, biography))
        if not candidates:
            continue
        biography = max(candidates, key=lambda item: item[0])[1]
        used_biographies.add(biography.block_id)
        group_id = f"p{inspection.page:03d}-profile-{index:03d}"
        coordinates = _box_union(photograph.coordinates, biography.coordinates)
        region = Region(
            region_id=group_id, page=inspection.page, kind="group", coordinates=coordinates,
            reading_order=min(
                int(photograph.provenance.get("reading_order", index)),
                int(biography.provenance.get("reading_order", index)),
            ),
            classification_method="aligned photograph-biography row grouping",
            confidence=min(photograph.confidence, biography.confidence),
        )
        group = SourceBlock(
            document_id=document_id, type="group", page=inspection.page, block_id=group_id,
            content={"role": "profile", "child_block_ids": [photograph.block_id, biography.block_id]},
            coordinates=coordinates, extraction_method=["Python profile-row hierarchy"],
            confidence=region.confidence,
            validation_status=(
                "passed" if photograph.validation_status == biography.validation_status == "passed"
                else "needs_review"
            ),
            warnings=(
                [] if photograph.validation_status == biography.validation_status == "passed"
                else ["one or more child blocks require review"]
            ),
            provenance=_provenance(source_hash, region, [], image), semantic_role="profile",
            child_block_ids=[photograph.block_id, biography.block_id],
        )
        insertion = min(blocks.index(photograph), blocks.index(biography))
        blocks.insert(insertion, group)
        photograph.parent_block_id = group_id
        photograph.semantic_role = "profile_image"
        biography.parent_block_id = group_id
        by_id = {block.block_id: block for block in blocks}
        _set_subtree_depth(photograph, 1, by_id)
        _set_subtree_depth(biography, 1, by_id)


def _arrange_page_blocks(blocks: list[SourceBlock]) -> None:
    """Put complete left and right reading lanes in human order before grouping."""
    roots = [block for block in blocks if block.parent_block_id is None]
    if len(roots) < 3:
        return
    lane_candidates = [block for block in roots if block.type != "heading" and block.coordinates[2] < 700]
    centers = sorted(block.coordinates[0] + block.coordinates[2] / 2 for block in lane_candidates)
    gaps = [(right - left, (left + right) / 2) for left, right in zip(centers, centers[1:])]
    if not gaps:
        return
    largest_gap, divider = max(gaps)
    if largest_gap < 120:
        return
    left = [block for block in roots if block.coordinates[0] + block.coordinates[2] / 2 < divider]
    right = [block for block in roots if block.coordinates[0] + block.coordinates[2] / 2 >= divider]
    if not left or not right:
        return
    headings = [block for block in roots if block.type == "heading" and block.coordinates[1] <= 220]
    remaining = [block for block in roots if block not in headings]
    ordered_roots = sorted(headings, key=lambda block: (block.coordinates[1], block.coordinates[0])) + sorted(
        remaining,
        key=lambda block: (
            0 if block.coordinates[0] + block.coordinates[2] / 2 < divider else 1,
            block.coordinates[1], block.coordinates[0],
        ),
    )
    by_parent: dict[str, list[SourceBlock]] = {}
    for block in blocks:
        if block.parent_block_id is not None:
            by_parent.setdefault(block.parent_block_id, []).append(block)
    ordered: list[SourceBlock] = []

    def append_tree(block: SourceBlock) -> None:
        ordered.append(block)
        for child in by_parent.get(block.block_id, []):
            append_tree(child)

    for root in ordered_roots:
        append_tree(root)
    blocks[:] = ordered


def _set_subtree_depth(block: SourceBlock, depth: int, by_id: dict[str, SourceBlock]) -> None:
    block.hierarchy_depth = depth
    for child_id in block.child_block_ids:
        child = by_id.get(child_id)
        if child is not None:
            _set_subtree_depth(child, depth + 1, by_id)


def _group_heading_led_sections(
    document_id: str, source_hash: str, inspection: PageInspection, image: Path,
    blocks: list[SourceBlock],
) -> None:
    """Create section parents from root headings and the root blocks that follow them."""
    roots = [block for block in blocks if block.parent_block_id is None]
    heading_positions = [index for index, block in enumerate(roots) if block.type == "heading"]
    if not heading_positions:
        return
    sections: list[tuple[int, list[SourceBlock]]] = []
    for section_index, start in enumerate(heading_positions, 1):
        end = heading_positions[section_index] if section_index < len(heading_positions) else len(roots)
        children = [
            block for block in roots[start:end]
            if block.semantic_role not in {"page_footer", "page_header"}
        ]
        if len(children) >= 2:
            sections.append((section_index, children))
    for section_index, children in sections:
        section_id = f"p{inspection.page:03d}-section-{section_index:03d}"
        coordinates = _line_box([{"coordinates": child.coordinates} for child in children])
        region = Region(
            region_id=section_id, page=inspection.page, kind="group", coordinates=coordinates,
            reading_order=children[0].provenance.get("reading_order", 1),
            classification_method="heading-led-section-grouping", confidence=min(child.confidence for child in children),
        )
        section = SourceBlock(
            document_id=document_id, type="group", page=inspection.page, block_id=section_id,
            content={"role": "section", "child_block_ids": [child.block_id for child in children]},
            coordinates=coordinates, extraction_method=["Python heading-led hierarchy"],
            confidence=region.confidence, validation_status="passed",
            provenance=_provenance(source_hash, region, [], image), semantic_role="document_section",
            child_block_ids=[child.block_id for child in children],
        )
        insertion = min(blocks.index(child) for child in children)
        blocks.insert(insertion, section)
        by_id = {block.block_id: block for block in blocks}
        for child in children:
            child.parent_block_id = section_id
            _set_subtree_depth(child, 1, by_id)


def _suppress_visual_observation_text_duplicates(blocks: list[SourceBlock]) -> None:
    """Remove native text copies of evidence already represented as visual observations.

    A narrow visual ownership box may leave an external chart label as a native
    text region. Suppress it only when its wording and location match a chart
    observation with separately owned OCR label and value evidence.
    """
    visuals = [block for block in blocks if block.type in {"chart", "map"}]
    duplicates: list[SourceBlock] = []
    for block in blocks:
        if block.type not in {"heading", "text", "footnote"} or block.parent_block_id is not None:
            continue
        if block.provenance.get("ocr_evidence_ids") or block.content.get("evidence_text", {}).get("selected") != "native":
            continue
        text = _fold_token(re.sub(r"\s+", " ", str(block.content.get("text") or "")).strip())
        if not text:
            continue
        bx, by, bw, bh = block.coordinates
        block_area = max(1.0, bw * bh)
        for visual in visuals:
            vx, vy, vw, vh = visual.coordinates
            overlap = (
                max(0.0, min(bx + bw, vx + vw) - max(bx, vx))
                * max(0.0, min(by + bh, vy + vh) - max(by, vy))
            ) / block_area
            if overlap < 0.50:
                continue
            observations = (
                visual.content.get("observations", []) if visual.type == "chart"
                else visual.content.get("bindings", [])
            )
            for observation in observations:
                label = str(observation.get("category") or observation.get("geography") or "").strip()
                value = str(observation.get("raw_value") or "").strip()
                if not label or not value or not observation.get("label_evidence_id") or not observation.get("value_evidence_id"):
                    continue
                possible_texts = {
                    _fold_token(re.sub(r"\s+", " ", candidate).strip())
                    for candidate in (label, value, f"{label} {value}")
                }
                if text not in possible_texts:
                    continue
                evidence_box = _box_union(
                    observation.get("label_coordinates"), observation.get("value_coordinates"),
                )
                if math.dist(_center(block.coordinates), _center(evidence_box)) <= max(60.0, max(bw, bh)):
                    duplicates.append(block)
                    break
            if block in duplicates:
                break
    if duplicates:
        blocks[:] = [block for block in blocks if block not in duplicates]


def _normalize_page_heading_roles(blocks: list[SourceBlock]) -> None:
    """One visual page title can coexist with subordinate numeric summaries."""
    titles = sorted(
        (block for block in blocks if block.type == "heading" and block.semantic_role == "page_title"),
        key=lambda block: (block.coordinates[1], block.coordinates[0]),
    )
    for block in titles[1:]:
        text = str(block.content.get("text") or "")
        block.semantic_role = "summary_heading" if NUMBER.search(text) and len(text.split()) <= 12 else "section_heading"
        block.heading_level = max(2, block.heading_level or 2)


def _normalize_block_reading_order(blocks: list[SourceBlock]) -> None:
    """Give parents and descendants a deterministic, unique page reading order."""
    for index, block in enumerate(blocks, 1):
        block.provenance["reading_order"] = index


def _propagate_group_validation(blocks: list[SourceBlock]) -> None:
    """A structural parent cannot claim to pass while one of its direct children needs review."""
    by_id = {block.block_id: block for block in blocks}
    groups = sorted(
        (block for block in blocks if block.type == "group"),
        key=lambda block: block.hierarchy_depth, reverse=True,
    )
    for group in groups:
        children = [by_id[child_id] for child_id in group.child_block_ids if child_id in by_id]
        if any(child.validation_status != "passed" for child in children):
            group.validation_status = "needs_review"
            if "one or more child blocks require review" not in group.warnings:
                group.warnings.append("one or more child blocks require review")


def _route_and_extract_page(
    document_id: str, source_hash: str, inspection: PageInspection, image: Path,
    ocr_page: dict[str, Any], crops: Path, native_threshold: float,
    route_threshold: float, qwen: QwenVisionClient | None,
    diagnostics: Path, document_token_pages: Counter[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    page_lines = _augment_native_table_evidence(
        inspection, _augment_native_visual_evidence(inspection, ocr_page.get("lines", [])),
    )
    ocr_page["lines"] = page_lines
    region_results = ocr_page.get("regions", {})
    blocks: list[SourceBlock] = []
    assigned: set[str] = set()
    route_errors: list[str] = []
    lines_by_region = _exclusive_region_lines(inspection.regions, page_lines)
    for region in inspection.regions:
        lines = lines_by_region.get(region.region_id, [])
        assigned.update(line["evidence_id"] for line in lines)
        features = region_results.get(region.region_id, {}).get("vision_features", {})
        if region.kind == "normal_text":
            structured = _inline_heading_subsection_blocks(
                document_id, source_hash, inspection, region, lines, image, native_threshold,
            )
            if not structured:
                structured = _profile_biography_blocks(
                    document_id, source_hash, inspection, region, lines, image, native_threshold,
                )
            if structured:
                blocks.extend(structured)
            else:
                blocks.append(_text_block(document_id, source_hash, inspection, region, lines, image, native_threshold))
            continue
        if region.kind == "table":
            crop_path = crops / f"{region.region_id}.png"
            _crop(image, region.coordinates, crop_path)
            table_payload = None
            table_error = None
            if qwen:
                try:
                    table_payload = qwen.analyze(
                        crop_path, _qwen_semantic_prompt("table", lines, region),
                    )
                    if table_payload.get("type") != "table":
                        table_error = "Qwen returned a non-table response during mandatory table review"
                        table_payload = None
                except Exception as exc:
                    table_error = f"{type(exc).__name__}: {exc}"
            blocks.extend(_table_blocks(
                document_id, source_hash, region, lines, image, table_payload, table_error,
            ))
            continue
        if region.kind == "decoration":
            crop_path = crops / f"{region.region_id}.png"
            _crop(image, region.coordinates, crop_path)
            vision_features_ref = _write_vision_diagnostic(
                diagnostics, document_id, source_hash, region, features,
            )
            semantic_band = _semantic_page_band_blocks(
                document_id, source_hash, inspection, region, lines, page_lines, image,
                crops, features, vision_features_ref, native_threshold, document_token_pages,
            )
            if semantic_band:
                blocks.extend(semantic_band)
                continue
            blocks.extend(_visual_blocks(
                document_id, source_hash, region, "decoration", region.confidence, [], lines,
                features, image, crop_path, None, vision_features_ref,
            ))
            continue
        kind, confidence, warnings = _classify_visual(region, lines, features)
        routing_payload = None
        crop_path = crops / f"{region.region_id}.png"
        _crop(image, region.coordinates, crop_path)
        vision_features_ref = _write_vision_diagnostic(
            diagnostics, document_id, source_hash, region, features,
        )
        if qwen and confidence < route_threshold:
            try:
                routing_payload = qwen.analyze(crop_path, (
                    "You are a document-region classifier. Return exactly one JSON object with exactly four "
                    "keys: type, confidence, chart_type, bindings. type must be exactly one of normal_text, "
                    "table, chart, map, photograph, brand_mark, unclassified_visual. confidence must be a JSON number from "
                    "zero to one. chart_type must be a short string or null. bindings must be a JSON array. "
                    "Do not return a transcription, a label-only object, Markdown, or commentary. Add bindings "
                    "only for clearly visible label and value pairs; otherwise return an empty array."
                ))
                proposed = str(routing_payload.get("type", "")).strip().lower()
                if proposed in {"normal_text", "table", "chart", "map", "photograph", "brand_mark", "unclassified_visual"}:
                    kind = proposed
                    confidence = float(routing_payload.get("confidence", confidence))
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
        if kind in {"unclassified_visual", "photograph"} and _looks_like_brand_mark(
            lines, page_lines, document_token_pages,
        ):
            kind = "brand_mark"
            confidence = max(0.82, min(confidence, 0.92))
            warnings = [
                warning for warning in warnings
                if warning != "visual type could not be classified confidently"
            ]
        semantic_payload = None
        semantic_error = None
        if qwen and kind in {"table", "chart", "map"}:
            try:
                if kind == "map":
                    value_ids = [str(line.get("evidence_id")) for line in _map_data_value_lines(lines)]
                    batches = [value_ids[index:index + 8] for index in range(0, len(value_ids), 8)] or [[]]
                    batch_payloads: list[dict[str, Any]] = []
                    batch_errors: list[str] = []
                    for batch_index, batch in enumerate(batches, 1):
                        try:
                            payload = qwen.analyze(
                                crop_path, _qwen_semantic_prompt(kind, lines, region, batch),
                                max_bindings=max(1, len(batch)),
                            )
                            if payload.get("type") == kind:
                                batch_payloads.append(payload)
                            else:
                                batch_errors.append(f"batch {batch_index}/{len(batches)} returned type {payload.get('type')!r}")
                        except Exception as exc:
                            batch_errors.append(f"batch {batch_index}/{len(batches)}: {type(exc).__name__}: {exc}")
                    if batch_payloads:
                        semantic_payload = {
                            "type": kind,
                            "confidence": min(float(payload.get("confidence", 0.0)) for payload in batch_payloads),
                            "chart_type": None,
                            "bindings": [
                                binding for payload in batch_payloads for binding in payload.get("bindings", [])
                            ],
                        }
                    if batch_errors:
                        semantic_error = "Qwen map semantic " + "; ".join(batch_errors)
                else:
                    semantic_payload = qwen.analyze(
                        crop_path, _qwen_semantic_prompt(kind, lines, region),
                        max_bindings=0 if kind == "table" else max(1, len(lines)),
                    )
                if semantic_payload is not None and semantic_payload.get("type") != kind:
                    semantic_error = f"Qwen returned type {semantic_payload.get('type')!r} during mandatory {kind} stage"
                    semantic_payload = None
                elif semantic_payload is None and semantic_error is None:
                    semantic_error = f"Qwen returned no usable payload during mandatory {kind} stage"
            except Exception as exc:
                semantic_error = f"{type(exc).__name__}: {exc}"
            if semantic_error:
                warnings.append(f"Qwen semantic stage unavailable: {semantic_error}")
        if kind == "normal_text":
            blocks.extend(_visual_text_panel_blocks(
                document_id, source_hash, inspection, region, lines, image, page_lines,
            ))
        elif kind == "table":
            blocks.extend(_table_blocks(
                document_id, source_hash, region, lines, image, semantic_payload, semantic_error,
            ))
        else:
            blocks.extend(_visual_blocks(
                document_id, source_hash, region, kind, confidence, warnings, lines, features,
                image, crop_path, semantic_payload, vision_features_ref,
            ))
    blocks.extend(_recover_unassigned_brand_marks(
        document_id, source_hash, inspection, image, page_lines, assigned, crops, document_token_pages,
    ))
    _suppress_visual_observation_text_duplicates(blocks)
    _group_profile_rows(document_id, source_hash, inspection, image, blocks)
    _attach_background_decorations(blocks)
    _arrange_page_blocks(blocks)
    _group_heading_led_sections(document_id, source_hash, inspection, image, blocks)
    _normalize_page_heading_roles(blocks)
    _propagate_group_validation(blocks)
    _normalize_block_reading_order(blocks)
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
    qwen_endpoint: str | None = None, qwen_model: str = "qwen3-vl:4b-instruct",
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
            "routing": "low-confidence classification; mandatory semantic stage for table/chart/map",
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
        manifest["preserved_source"] = f"source/{copied.name}"
        manifest["stages"].append({"name": "ingestion", "status": "complete", "finished_utc": _now()})
        _write_json(output / "manifest.json", manifest)

        inspections, pdf_metadata = inspect_pdf(pdf)
        selected_inspections = [inspection for inspection in inspections if inspection.page in pages]
        inspection_records = []
        for page_inspection in selected_inspections:
            page_inspection_path = inspection_dir / f"page-{page_inspection.page:03d}.json"
            _write_json(page_inspection_path, {
                "schema_version": INSPECTION_SCHEMA_VERSION,
                "document_id": document_id,
                "source_sha256": source_hash,
                **page_inspection.as_dict(),
            })
            inspection_records.append({
                "page": page_inspection.page,
                "file": page_inspection_path.name,
                "sha256": _sha256(page_inspection_path),
                "region_count": len(page_inspection.regions),
            })
        inspection_path = inspection_dir / "manifest.json"
        _write_json(inspection_path, {
            "schema_version": INSPECTION_INDEX_SCHEMA_VERSION,
            "document_id": document_id, "source_sha256": source_hash,
            "page_count": page_count, "selected_pages": pages, "pdf_metadata": pdf_metadata,
            "pages": inspection_records,
        })
        manifest["inspection"] = {
            "path": str(inspection_path.relative_to(output)).replace("\\", "/"),
            "sha256": _sha256(inspection_path),
            "schema_version": INSPECTION_INDEX_SCHEMA_VERSION,
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
        document_token_pages = _document_token_page_frequency(ocr_by_page, pdf.stem)
        page_records = []
        type_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        collection_errors: list[str] = []
        for inspection in selected_inspections:
            ocr_page = ocr_by_page.get(inspection.page, {"lines": [], "regions": {}})
            blocks, unassigned, errors = _route_and_extract_page(
                document_id, source_hash, inspection, rendered[inspection.page],
                ocr_page,
                crops_dir, native_threshold, route_threshold, qwen, diagnostics_dir, document_token_pages,
            )
            for block in blocks:
                type_counts[block["type"]] += 1
                status_counts[block["validation"]["status"]] += 1
            completeness_warnings: list[str] = []
            high_confidence_unassigned = [
                item for item in unassigned
                if float(item.get("evidence", {}).get("confidence", 0.0)) >= 0.80
                and re.search(r"[A-Za-z0-9]", str(item.get("evidence", {}).get("text", "")))
            ]
            if high_confidence_unassigned:
                completeness_warnings.append(
                    f"{len(high_confidence_unassigned)} high-confidence OCR lines remain semantically unassigned"
                )
            review_blocks = [
                block for block in blocks if block.get("validation", {}).get("status") != "passed"
            ]
            if review_blocks:
                completeness_warnings.append(f"{len(review_blocks)} source blocks require review")
            completeness_status = "complete" if not completeness_warnings else "needs_review"
            page_payload = {
                "schema_version": PAGE_SCHEMA_VERSION, "document_id": document_id,
                "source_sha256": source_hash, "page": inspection.page,
                "blocks": blocks,
                "evidence_ledger": {
                    "rendered_page": f"page-images/{rendered[inspection.page].name}",
                    "rendered_page_sha256": _sha256(rendered[inspection.page]),
                    "ocr_engine": ocr_result.get("engine"),
                    "ocr_lines": ocr_page.get("lines", []),
                },
                "evidence_disposition": [
                    {
                        "evidence_id": line["evidence_id"],
                        "owner_block_id": next((
                            block["block_id"] for block in blocks
                            if line["evidence_id"] in block.get("provenance", {}).get("ocr_evidence_ids", [])
                        ), None),
                        "status": "owned" if any(
                            line["evidence_id"] in block.get("provenance", {}).get("ocr_evidence_ids", [])
                            for block in blocks
                        ) else "unassigned",
                    }
                    for line in ocr_page.get("lines", [])
                ],
                "unassigned_evidence": unassigned,
                "validation": {
                    "valid": not errors, "errors": errors,
                    "completeness_status": completeness_status,
                    "warnings": completeness_warnings,
                },
            }
            page_path = blocks_dir / f"page-{inspection.page:03d}.json"
            _write_json(page_path, page_payload)
            page_records.append({
                "page": inspection.page, "file": page_path.name, "sha256": _sha256(page_path),
                "block_count": len(blocks), "unassigned_evidence_count": len(unassigned),
                "valid": not errors, "completeness_status": completeness_status,
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
        manifest["source_blocks_manifest"] = "source-blocks/document-manifest.json"
        manifest["validation"] = collection_manifest["validation"]
        _write_json(output / "manifest.json", manifest)
        return output
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["finished_utc"] = _now()
        manifest["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        _write_json(output / "manifest.json", manifest)
        raise

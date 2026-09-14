from __future__ import annotations

import re
import statistics
from pathlib import Path
from typing import Any

import pdfplumber

from .models import PageInspection, Region


def _normalized_xywh(bbox: tuple[float, float, float, float], width: float, height: float) -> list[float]:
    x0, top, x1, bottom = bbox
    return [
        max(0.0, min(1000.0, x0 * 1000.0 / width)),
        max(0.0, min(1000.0, top * 1000.0 / height)),
        max(0.0, min(1000.0, (x1 - x0) * 1000.0 / width)),
        max(0.0, min(1000.0, (bottom - top) * 1000.0 / height)),
    ]


def _center_in(bbox: tuple[float, float, float, float], other: tuple[float, float, float, float]) -> bool:
    x0, top, x1, bottom = bbox
    ox0, otop, ox1, obottom = other
    cx, cy = (x0 + x1) / 2, (top + bottom) / 2
    return ox0 <= cx <= ox1 and otop <= cy <= obottom


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _overlap_ratio(
    bbox: tuple[float, float, float, float], other: tuple[float, float, float, float],
) -> float:
    intersection_width = max(0.0, min(bbox[2], other[2]) - max(bbox[0], other[0]))
    intersection_height = max(0.0, min(bbox[3], other[3]) - max(bbox[1], other[1]))
    return intersection_width * intersection_height / max(1.0, min(_area(bbox), _area(other)))


def _expanded_visual_bbox(
    members: list[tuple[float, float, float, float]], page_width: float, page_height: float,
) -> tuple[float, float, float, float]:
    x0 = min(bbox[0] for bbox in members)
    top = min(bbox[1] for bbox in members)
    x1 = max(bbox[2] for bbox in members)
    bottom = max(bbox[3] for bbox in members)
    return (
        max(0.0, x0 - page_width * 0.08),
        max(0.0, top - page_height * 0.10),
        min(page_width, x1 + page_width * 0.08),
        min(page_height * 0.87, bottom + page_height * 0.14),
    )


def detect_vector_bar_chart(
    rectangles: list[dict[str, Any]], page_width: float, page_height: float,
) -> dict[str, Any] | None:
    """Find a repeated set of filled bars sharing a page-space baseline."""
    candidates: list[tuple[float, float, float, float]] = []
    for rectangle in rectangles:
        try:
            bbox = tuple(float(rectangle[key]) for key in ("x0", "top", "x1", "bottom"))
        except (KeyError, TypeError, ValueError):
            continue
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if (
            rectangle.get("fill") is True
            and 3.0 <= width <= page_width * 0.12
            and 15.0 <= height <= page_height * 0.70
            and bbox[1] >= page_height * 0.15
            and bbox[3] <= page_height * 0.87
        ):
            candidates.append(bbox)
    if len(candidates) < 4:
        return None
    baseline_groups: list[list[tuple[float, float, float, float]]] = []
    for candidate in sorted(candidates, key=lambda bbox: bbox[3]):
        group = next(
            (items for items in baseline_groups if abs(statistics.median(item[3] for item in items) - candidate[3]) <= 3.0),
            None,
        )
        if group is None:
            baseline_groups.append([candidate])
        else:
            group.append(candidate)
    bars = max(baseline_groups, key=len, default=[])
    distinct_centers = {round((bbox[0] + bbox[2]) / 2, 1) for bbox in bars}
    if len(bars) < 4 or len(distinct_centers) < 4:
        return None
    bars = sorted(bars, key=lambda bbox: bbox[0])
    return {
        "bbox": _expanded_visual_bbox(bars, page_width, page_height),
        "source_member_bboxes": [list(bbox) for bbox in bars],
        "member_coordinates": [_normalized_xywh(bbox, page_width, page_height) for bbox in bars],
        "object_family": "pdf-vector-bar-cluster",
        "visual_hint": "chart",
        "chart_type_hint": "bar",
        "expected_mark_count": len(bars),
        "confidence": 0.94,
    }


def detect_small_raster_chart(
    images: list[tuple[float, float, float, float]], page_width: float, page_height: float,
) -> dict[str, Any] | None:
    """Find charts composed from many small raster objects (pie labels or scatter points)."""
    candidates = [
        bbox for bbox in images
        if bbox[1] < page_height * 0.85
        and _area(bbox) / max(1.0, page_width * page_height) <= 0.025
        and bbox[2] - bbox[0] <= page_width * 0.35
        and bbox[3] - bbox[1] <= page_height * 0.15
    ]
    if len(candidates) < 8:
        return None
    point_marks = [
        bbox for bbox in candidates
        if _area(bbox) / max(1.0, page_width * page_height) <= 0.002
        and min(bbox[2] - bbox[0], bbox[3] - bbox[1]) / max(1.0, bbox[2] - bbox[0], bbox[3] - bbox[1]) >= 0.70
    ]
    scatterplot = len(point_marks) >= 8
    marks = point_marks if scatterplot else []
    return {
        "bbox": _expanded_visual_bbox(candidates, page_width, page_height),
        "image_indices": [],
        "source_member_bboxes": [list(bbox) for bbox in candidates],
        "member_coordinates": [_normalized_xywh(bbox, page_width, page_height) for bbox in marks],
        "object_family": "pdf-raster-point-cluster" if scatterplot else "pdf-raster-object-cluster",
        "visual_hint": "chart",
        "chart_type_hint": "scatterplot" if scatterplot else "pie",
        "expected_mark_count": len(marks) if scatterplot else None,
        "object_count": len(candidates),
        "confidence": 0.95 if scatterplot else 0.84,
    }


def _join_table_cell(words: list[dict[str, Any]], numeric: bool = False) -> str:
    text = " ".join(str(word.get("text", "")).strip() for word in sorted(words, key=lambda word: float(word["x0"]))).strip()
    if numeric:
        text = text.replace("$ ", "$")
        text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    return text


def _group_positioned_lines(words: list[dict[str, Any]], tolerance: float = 2.5) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        group = next(
            (items for items in groups if abs(statistics.median(float(item["top"]) for item in items) - float(word["top"])) <= tolerance),
            None,
        )
        if group is None:
            groups.append([word])
        else:
            group.append(word)
    return groups


def _reading_order_text(words: list[dict[str, Any]]) -> str:
    return " ".join(
        _join_table_cell(line) for line in _group_positioned_lines(words) if line
    ).strip()


def _words_bbox(words: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    if not words:
        return None
    return (
        min(float(word["x0"]) for word in words), min(float(word["top"]) for word in words),
        max(float(word["x1"]) for word in words), max(float(word["bottom"]) for word in words),
    )


def _merge_numeric_fragments(
    words: list[dict[str, Any]], page_width: float,
) -> list[dict[str, Any]]:
    """Join adjacent native-text number fragments before assigning table columns."""
    numeric_fragment = re.compile(r"^\d[\d,.]*$")
    ordered = sorted(words, key=lambda word: float(word["x0"]))
    merged: list[dict[str, Any]] = []
    index = 0
    while index < len(ordered):
        current = dict(ordered[index])
        current_text = str(current.get("text", "")).strip()
        while index + 1 < len(ordered):
            following = ordered[index + 1]
            following_text = str(following.get("text", "")).strip()
            gap = float(following["x0"]) - float(current["x1"])
            short_numeric_prefix = (
                numeric_fragment.fullmatch(current_text) is not None and len(current_text) <= 2
            ) or re.fullmatch(r"[$€£]\d{1,2}", current_text) is not None
            can_join_digits = (
                short_numeric_prefix
                and numeric_fragment.fullmatch(following_text) is not None
                and -1.5 <= gap <= page_width * 0.012
            )
            # Financial PDFs frequently position a currency glyph independently at
            # the left edge of a numeric column.  Attach it before column ownership
            # is calculated so it cannot leak into the percentage column on its left.
            can_join_currency = (
                current_text in {"$", "€", "£"}
                and re.fullmatch(r"\(?[-+]?\d[\d,.]*\)?", following_text) is not None
                and -1.5 <= gap <= page_width * 0.035
            )
            can_join = can_join_digits or can_join_currency
            if not can_join:
                break
            current["text"] = current_text + following_text
            current["x1"] = max(float(current["x1"]), float(following["x1"]))
            current["top"] = min(float(current["top"]), float(following["top"]))
            current["bottom"] = max(float(current["bottom"]), float(following["bottom"]))
            current_text = str(current["text"])
            index += 1
        merged.append(current)
        index += 1
    return merged


def _header_phrases(words: list[dict[str, Any]], page_width: float) -> list[dict[str, Any]]:
    """Join visually contiguous header words before assigning a column.

    Assigning individual words by their centres splits phrases that straddle an
    inferred boundary (for example ``Leverage (CCC Debt)``).  Phrase ownership is
    both more stable and more faithful to how multi-line table headers are drawn.
    """
    phrases: list[dict[str, Any]] = []
    for line in _group_positioned_lines(words):
        current: list[dict[str, Any]] = []
        for word in sorted(line, key=lambda item: float(item["x0"])):
            if current and float(word["x0"]) - float(current[-1]["x1"]) > page_width * 0.022:
                phrases.append({
                    "text": _join_table_cell(current),
                    "x0": min(float(item["x0"]) for item in current),
                    "x1": max(float(item["x1"]) for item in current),
                    "top": min(float(item["top"]) for item in current),
                    "bottom": max(float(item["bottom"]) for item in current),
                })
                current = []
            current.append(word)
        if current:
            phrases.append({
                "text": _join_table_cell(current),
                "x0": min(float(item["x0"]) for item in current),
                "x1": max(float(item["x1"]) for item in current),
                "top": min(float(item["top"]) for item in current),
                "bottom": max(float(item["bottom"]) for item in current),
            })
    return phrases


def _numeric_column_anchors(
    words: list[dict[str, Any]], page_width: float,
) -> list[float]:
    numeric_token = re.compile(
        r"^(?:[-–—]|\(?[-+]?\d[\d,.]*(?:%|x|mm|m|b|k)?\)?)$",
        re.I,
    )
    numeric_words = [
        word for word in words
        if numeric_token.fullmatch(str(word.get("text", "")).strip())
    ]
    endpoints = []
    for word in numeric_words:
        text = str(word.get("text", "")).strip()
        is_leading_fragment = (
            text.isdigit() and len(text) <= 2
            and any(
                other is not word
                and abs(float(other["top"]) - float(word["top"])) <= 2.5
                and float(word["x1"]) - 1.0 <= float(other["x0"]) <= float(word["x1"]) + page_width * 0.012
                and re.match(r"^\d[\d,.]+", str(other.get("text", "")).strip()) is not None
                for other in numeric_words
            )
        )
        if not is_leading_fragment:
            endpoints.append(float(word["x1"]))
    clusters: list[list[float]] = []
    for endpoint in sorted(endpoints):
        cluster = next(
            (items for items in clusters if abs(statistics.median(items) - endpoint) <= page_width * 0.012),
            None,
        )
        if cluster is None:
            clusters.append([endpoint])
        else:
            cluster.append(endpoint)
    repeated = [
        (statistics.median(cluster), len(cluster)) for cluster in clusters if len(cluster) >= 4
    ]
    anchors: list[tuple[float, int]] = []
    for center, count in sorted(repeated, key=lambda item: item[0]):
        if anchors and center - anchors[-1][0] < page_width * 0.04:
            if count > anchors[-1][1]:
                anchors[-1] = (center, count)
        else:
            anchors.append((center, count))
    return [center for center, _count in anchors]


def detect_aligned_financial_table(
    words: list[dict[str, Any]], rectangles: list[dict[str, Any]], page_width: float, page_height: float,
) -> dict[str, Any] | None:
    """Infer a financial table from a header band and repeated numeric alignment."""
    header_rectangles: list[tuple[float, float, float, float]] = []
    for rectangle in rectangles:
        try:
            bbox = tuple(float(rectangle[key]) for key in ("x0", "top", "x1", "bottom"))
        except (KeyError, TypeError, ValueError):
            continue
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if (
            rectangle.get("fill") is True and width >= page_width * 0.80
            and page_height * 0.05 <= height <= page_height * 0.15
            and page_height * 0.15 <= bbox[1] <= page_height * 0.40
        ):
            header_rectangles.append(bbox)
    if not header_rectangles:
        return None
    header = min(header_rectangles, key=lambda bbox: bbox[1])
    provisional_words = [
        word for word in words
        if header[3] < float(word["top"]) <= page_height * 0.85
        and header[0] <= (float(word["x0"]) + float(word["x1"])) / 2 <= header[2]
    ]
    anchors = _numeric_column_anchors(provisional_words, page_width)
    if not 2 <= len(anchors) <= 12:
        return None
    gaps = [right - left for left, right in zip(anchors, anchors[1:])]
    typical_gap = statistics.median(gaps) if gaps else (header[2] - header[0]) / (len(anchors) + 1)
    first_numeric_left = max(header[0] + page_width * 0.08, anchors[0] - typical_gap * 0.90)
    boundaries = [header[0], first_numeric_left]
    boundaries.extend((left + right) / 2 for left, right in zip(anchors, anchors[1:]))
    boundaries.append(header[2])
    if any(right - left < page_width * 0.025 for left, right in zip(boundaries, boundaries[1:])):
        return None

    band_bottoms = [
        float(rectangle.get("bottom", 0)) for rectangle in rectangles
        if float(rectangle.get("x1", 0)) - float(rectangle.get("x0", 0)) >= page_width * 0.80
        and header[3] <= float(rectangle.get("bottom", 0)) <= page_height * 0.85
    ]
    aligned_word_bottoms = [
        float(word["bottom"]) for word in provisional_words
        if any(abs(float(word["x1"]) - anchor) <= page_width * 0.02 for anchor in anchors)
    ]
    bottom = max(band_bottoms + aligned_word_bottoms, default=header[3])
    if bottom - header[3] < page_height * 0.25:
        return None
    column_count = len(boundaries) - 1
    header_words = [
        word for word in words
        if header[1] <= float(word["top"]) <= header[3]
        and header[0] <= (float(word["x0"]) + float(word["x1"])) / 2 <= header[2]
    ]
    header_columns: list[list[dict[str, Any]]] = [[] for _ in range(column_count)]
    for phrase in _header_phrases(header_words, page_width):
        center = (float(phrase["x0"]) + float(phrase["x1"])) / 2
        index = next((i for i in range(column_count) if boundaries[i] <= center <= boundaries[i + 1]), column_count - 1)
        header_columns[index].append(phrase)
    headers = [
        re.sub(r"(?<=\w)-\s+(?=\w)", "-", _reading_order_text(column))
        for column in header_columns
    ]
    cell_coordinates: list[list[list[float] | None]] = [[
        _normalized_xywh(bbox, page_width, page_height) if (bbox := _words_bbox(column)) else None
        for column in header_columns
    ]]
    if any(not header_value for header_value in headers):
        return None
    body_words = [
        word for word in words
        if header[3] < float(word["top"]) <= bottom and header[0] <= (float(word["x0"]) + float(word["x1"])) / 2 <= header[2]
    ]
    line_groups = _group_positioned_lines(body_words)
    rows: list[list[str]] = [headers]
    row_sections: list[str | None] = [None]
    section: str | None = None
    for group in line_groups:
        group = _merge_numeric_fragments(group, page_width)
        columns: list[list[dict[str, Any]]] = [[] for _ in range(column_count)]
        for word in group:
            center = (float(word["x0"]) + float(word["x1"])) / 2
            index = next((i for i in range(column_count) if boundaries[i] <= center <= boundaries[i + 1]), column_count - 1)
            columns[index].append(word)
        values = [_join_table_cell(column, numeric=index > 0) for index, column in enumerate(columns)]
        if values[0] and not any(character.isdigit() for character in values[0]) and sum(bool(value) for value in values) == 1:
            section = values[0]
            continue
        if sum(bool(value) for value in values) < 2:
            continue
        rows.append(values)
        row_sections.append(section)
        cell_coordinates.append([
            _normalized_xywh(bbox, page_width, page_height) if (bbox := _words_bbox(column)) else None
            for column in columns
        ])
    if len(rows) < 6:
        return None
    title_candidates = [
        line for line in _group_positioned_lines(words)
        if line and header[1] - page_height * 0.16 <= statistics.median(float(word["top"]) for word in line) < header[1]
    ]
    title = _reading_order_text(max(title_candidates, key=lambda line: statistics.median(float(word["top"]) for word in line))) if title_candidates else None
    return {
        "bbox": (header[0], header[1], header[2], bottom),
        "rows": rows,
        "cell_coordinates": cell_coordinates,
        "row_sections": row_sections,
        "title": title,
        "column_anchors_points": anchors,
        "confidence": 0.96,
        "classification_method": "native-positioned-aligned-financial-table",
    }


def _projection_overlap(start1: float, end1: float, start2: float, end2: float) -> float:
    intersection = max(0.0, min(end1, end2) - max(start1, start2))
    return intersection / max(1.0, min(end1 - start1, end2 - start2))


def _visuals_belong_together(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float],
    page_width: float, page_height: float,
) -> bool:
    # Wide bottom bands are normally repeated branding, not part of the analytical visual above.
    if any(bbox[1] >= page_height * 0.84 and (bbox[2] - bbox[0]) >= page_width * 0.70 for bbox in (left, right)):
        return False
    ix = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    iy = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    if ix > 0 and iy > 0:
        return ix * iy / max(1.0, min(_area(left), _area(right))) >= 0.01
    horizontal_gap = max(0.0, max(left[0], right[0]) - min(left[2], right[2]))
    vertical_gap = max(0.0, max(left[1], right[1]) - min(left[3], right[3]))
    horizontally_adjacent = (
        horizontal_gap <= page_width * 0.015
        and _projection_overlap(left[1], left[3], right[1], right[3]) >= 0.35
    )
    vertically_adjacent = (
        vertical_gap <= page_height * 0.015
        and _projection_overlap(left[0], left[2], right[0], right[2]) >= 0.35
    )
    return horizontally_adjacent or vertically_adjacent


def merge_visual_objects(
    images: list[tuple[float, float, float, float]], page_width: float, page_height: float,
) -> list[dict[str, Any]]:
    """Merge connected raster objects while keeping unrelated visuals separate."""
    remaining = set(range(len(images)))
    components: list[list[int]] = []
    while remaining:
        seed = remaining.pop()
        component = [seed]
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            attached = [
                candidate for candidate in remaining
                if _visuals_belong_together(images[current], images[candidate], page_width, page_height)
            ]
            for candidate in attached:
                remaining.remove(candidate)
                component.append(candidate)
                frontier.append(candidate)
        components.append(sorted(component))
    merged: list[dict[str, Any]] = []
    for component in components:
        members = [images[index] for index in component]
        x0 = min(bbox[0] for bbox in members)
        top = min(bbox[1] for bbox in members)
        x1 = max(bbox[2] for bbox in members)
        bottom = max(bbox[3] for bbox in members)
        if len(component) > 1:
            # Include external labels and a title while avoiding the repeated footer below.
            x0 = max(0.0, x0 - page_width * 0.12)
            x1 = min(page_width, x1 + page_width * 0.12)
            top = max(0.0, top - page_height * 0.08)
        merged.append({
            "bbox": (x0, top, x1, bottom),
            "image_indices": [index + 1 for index in component],
            "source_member_bboxes": [list(bbox) for bbox in members],
        })
    return sorted(merged, key=lambda item: (item["bbox"][1], item["bbox"][0]))


def mark_repeated_decorations(inspections: list[PageInspection]) -> None:
    """Classify repeated full-width footer/header image bands without discarding them."""
    candidates: dict[tuple[int, int, int, int], list[Region]] = {}
    for inspection in inspections:
        for region in inspection.regions:
            x, y, width, height = region.coordinates
            is_band = width >= 700 and height <= 180 and (y >= 820 or y + height <= 180)
            if region.kind != "visual" or not is_band:
                continue
            signature = (round(x / 25), round(y / 25), round(width / 25), round(height / 25))
            candidates.setdefault(signature, []).append(region)
    for regions in candidates.values():
        pages = sorted({region.page for region in regions})
        if len(pages) < 3:
            continue
        for region in regions:
            region.kind = "decoration"
            region.classification_method = "repeated-page-band"
            region.confidence = 0.98
            region.metadata["repeated_on_pages"] = pages


def _native_quality(chars: list[dict[str, Any]], page_area: float) -> tuple[float, float]:
    if not chars:
        return 0.0, 0.0
    texts = [str(char.get("text", "")) for char in chars]
    joined = "".join(texts)
    printable = sum(character.isprintable() and character != "\ufffd" for character in joined) / max(1, len(joined))
    keys = [
        (text, round(float(char.get("x0", 0)), 1), round(float(char.get("top", 0)), 1))
        for text, char in zip(texts, chars)
        if text.strip()
    ]
    duplicate_ratio = 1 - len(set(keys)) / max(1, len(keys))
    char_area = sum(
        max(0.0, float(char.get("x1", 0)) - float(char.get("x0", 0)))
        * max(0.0, float(char.get("bottom", 0)) - float(char.get("top", 0)))
        for char in chars
    )
    coverage = min(1.0, char_area / max(1.0, page_area))
    length_score = min(1.0, len(joined.strip()) / 120.0)
    quality = 0.45 * printable + 0.35 * (1 - duplicate_ratio) + 0.20 * length_score
    return round(coverage, 5), round(max(0.0, min(1.0, quality)), 5)


def _group_words_single(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert one reading-order lane into conservative paragraph regions."""
    if not words:
        return []
    ordered = sorted(words, key=lambda word: (round(float(word["top"]), 1), float(word["x0"])))
    heights = [max(1.0, float(word["bottom"]) - float(word["top"])) for word in ordered]
    median_height = statistics.median(heights)
    lines: list[list[dict[str, Any]]] = []
    for word in ordered:
        if not lines:
            lines.append([word])
            continue
        current_top = statistics.median(float(item["top"]) for item in lines[-1])
        if abs(float(word["top"]) - current_top) <= max(2.0, median_height * 0.35):
            lines[-1].append(word)
        else:
            lines.append([word])
    paragraphs: list[list[list[dict[str, Any]]]] = []
    for line in lines:
        line.sort(key=lambda word: float(word["x0"]))
        if not paragraphs:
            paragraphs.append([line])
            continue
        previous = paragraphs[-1][-1]
        previous_bottom = max(float(word["bottom"]) for word in previous)
        current_top = min(float(word["top"]) for word in line)
        previous_left = min(float(word["x0"]) for word in previous)
        current_left = min(float(word["x0"]) for word in line)
        gap = current_top - previous_bottom
        if gap <= max(9.0, median_height * 1.25) and abs(current_left - previous_left) <= max(45.0, median_height * 4):
            paragraphs[-1].append(line)
        else:
            paragraphs.append([line])
    result = []
    for paragraph in paragraphs:
        flat = [word for line in paragraph for word in line]
        text = "\n".join(" ".join(str(word["text"]) for word in line) for line in paragraph)
        sizes = [float(word.get("size", 0) or 0) for word in flat]
        result.append({
            "text": text,
            "bbox": (
                min(float(word["x0"]) for word in flat),
                min(float(word["top"]) for word in flat),
                max(float(word["x1"]) for word in flat),
                max(float(word["bottom"]) for word in flat),
            ),
            "median_font_size": statistics.median(sizes) if sizes else 0.0,
            "word_count": len(flat),
        })
    return result


def _group_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group words while preserving a clear two-column reading order."""
    if not words:
        return []
    left_edge = min(float(word["x0"]) for word in words)
    right_edge = max(float(word["x1"]) for word in words)
    divider = (left_edge + right_edge) / 2
    gutter = max(5.0, (right_edge - left_edge) * 0.012)
    left = [word for word in words if float(word["x1"]) <= divider + gutter]
    right = [word for word in words if float(word["x0"]) >= divider - gutter]
    spanning = [word for word in words if word not in left and word not in right]
    total = len(words)
    if len(left) >= 20 and len(right) >= 20 and len(spanning) <= max(2, round(total * 0.03)):
        # Human reading order for a two-column page is the complete left lane,
        # followed by the complete right lane.  Full-width headings remain first.
        grouped_spanning = _group_words_single(spanning)
        grouped_left = _group_words_single(left)
        grouped_right = _group_words_single(right)
        grouped_spanning.sort(key=lambda item: item["bbox"][1])
        return grouped_spanning + grouped_left + grouped_right
    return _group_words_single(words)


def inspect_pdf(pdf_path: Path) -> tuple[list[PageInspection], dict[str, Any]]:
    inspections: list[PageInspection] = []
    with pdfplumber.open(pdf_path) as document:
        metadata = dict(document.metadata or {})
        for page_number, page in enumerate(document.pages, 1):
            width, height = float(page.width), float(page.height)
            area = width * height
            chars = list(page.chars or [])
            coverage, quality = _native_quality(chars, area)
            warnings: list[str] = []
            try:
                detected_tables = page.find_tables()
            except Exception as exc:  # malformed PDFs must remain inspectable
                detected_tables = []
                warnings.append(f"table_detection_failed: {type(exc).__name__}: {exc}")
            table_candidates: list[tuple[Any, tuple[float, float, float, float], list[list[Any]]]] = []
            for table_index, table in enumerate(detected_tables, 1):
                bbox = tuple(map(float, table.bbox))
                try:
                    rows = table.extract() or []
                except Exception as exc:
                    rows = []
                    warnings.append(f"table_extract_failed_{table_index}: {type(exc).__name__}: {exc}")
                width_ratio = (bbox[2] - bbox[0]) / width
                height_ratio = (bbox[3] - bbox[1]) / height
                # Decorative page frames frequently look like a one-cell table. They must not
                # swallow every real region on the page.
                if width_ratio >= 0.96 and height_ratio >= 0.96 and len(rows) <= 4:
                    warnings.append(f"ignored_full_page_table_false_positive_{table_index}")
                    continue
                table_candidates.append((table, bbox, rows))
            all_images = []
            large_images = []
            for image in page.images or []:
                try:
                    bbox = (float(image["x0"]), float(image["top"]), float(image["x1"]), float(image["bottom"]))
                    all_images.append(bbox)
                    if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / area >= 0.03:
                        large_images.append(bbox)
                except (KeyError, TypeError, ValueError):
                    warnings.append("image_with_invalid_bbox")
            image_coverage = min(1.0, sum((x1 - x0) * (bottom - top) for x0, top, x1, bottom in large_images) / area)
            vector_count = len(page.lines or []) + len(page.rects or []) + len(page.curves or [])
            positioned_words = page.extract_words(extra_attrs=["size"], use_text_flow=False) or []
            aligned_table = detect_aligned_financial_table(
                positioned_words, list(page.rects or []), width, height,
            )

            object_visuals = [
                visual for visual in (
                    detect_vector_bar_chart(list(page.rects or []), width, height),
                    detect_small_raster_chart(all_images, width, height),
                ) if visual is not None
            ]
            retained_tables = []
            for table, bbox, rows in table_candidates:
                cell_count = sum(len(row) for row in rows)
                populated = sum(bool(str(cell or "").strip()) for row in rows for cell in row)
                density = populated / max(1, cell_count)
                overlaps_chart = any(_overlap_ratio(bbox, tuple(visual["bbox"])) >= 0.50 for visual in object_visuals)
                if overlaps_chart and density < 0.50:
                    warnings.append("ignored_sparse_table_overlapping_detected_chart")
                    continue
                if aligned_table and _overlap_ratio(bbox, tuple(aligned_table["bbox"])) >= 0.50:
                    warnings.append("ignored_fragment_table_inside_aligned_financial_table")
                    continue
                retained_tables.append((table, bbox, rows))
            table_candidates = retained_tables
            table_bboxes = ([tuple(aligned_table["bbox"])] if aligned_table else []) + [candidate[1] for candidate in table_candidates]

            regions: list[Region] = []
            order = 0
            if aligned_table:
                order += 1
                regions.append(Region(
                    region_id=f"p{page_number:03d}-r{order:03d}", page=page_number,
                    kind="table", coordinates=_normalized_xywh(tuple(aligned_table["bbox"]), width, height),
                    reading_order=order, classification_method=str(aligned_table["classification_method"]),
                    confidence=float(aligned_table["confidence"]), metadata={
                        "rows": aligned_table["rows"],
                        "cell_coordinates": aligned_table["cell_coordinates"],
                        "row_sections": aligned_table["row_sections"],
                        "title": aligned_table["title"],
                        "column_anchors_points": aligned_table["column_anchors_points"],
                        "source_bbox_points": list(aligned_table["bbox"]),
                    },
                ))
                warnings.append("reconstructed_native_aligned_financial_table")
            for index, (_table, bbox, rows) in enumerate(table_candidates, 1):
                order += 1
                regions.append(Region(
                    region_id=f"p{page_number:03d}-r{order:03d}", page=page_number,
                    kind="table", coordinates=_normalized_xywh(bbox, width, height),
                    reading_order=order, classification_method="pdfplumber-table-finder",
                    confidence=0.92 if len(rows) >= 2 else 0.68,
                    metadata={"rows": rows, "source_bbox_points": list(bbox)},
                ))

            logical_visuals = merge_visual_objects(large_images, width, height)
            # Full-page scan images are represented once, not as a duplicate visual over native text.
            for visual in logical_visuals:
                bbox = visual["bbox"]
                ratio = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / area
                if ratio > 0.90 and quality >= 0.72:
                    continue
                order += 1
                image_indices = visual["image_indices"]
                method = "pdf-image-object-merge" if len(image_indices) > 1 else "pdf-image-object"
                regions.append(Region(
                    region_id=f"p{page_number:03d}-r{order:03d}", page=page_number,
                    kind="visual", coordinates=_normalized_xywh(bbox, width, height),
                    reading_order=order, classification_method=method,
                    confidence=0.72 if len(image_indices) > 1 else 0.58,
                    metadata={
                        "image_indices": image_indices,
                        "source_bbox_points": list(bbox),
                        "source_member_bboxes": visual["source_member_bboxes"],
                        "member_coordinates": [
                            _normalized_xywh(tuple(member), width, height)
                            for member in visual["source_member_bboxes"]
                        ],
                        "logical_visual_merge": len(image_indices) > 1,
                    },
                ))
                if len(image_indices) > 1:
                    warnings.append(f"merged_{len(image_indices)}_image_objects_into_1_visual_region")

            existing_visual_bboxes = [
                tuple(region.metadata["source_bbox_points"])
                for region in regions if region.kind == "visual"
            ]
            for visual in object_visuals:
                bbox = tuple(visual["bbox"])
                if any(_overlap_ratio(bbox, existing) >= 0.50 for existing in existing_visual_bboxes):
                    continue
                order += 1
                metadata = {key: value for key, value in visual.items() if key not in {"bbox", "confidence"}}
                metadata["source_bbox_points"] = list(bbox)
                regions.append(Region(
                    region_id=f"p{page_number:03d}-r{order:03d}", page=page_number,
                    kind="visual", coordinates=_normalized_xywh(bbox, width, height),
                    reading_order=order, classification_method=str(visual["object_family"]),
                    confidence=float(visual["confidence"]), metadata=metadata,
                ))
                existing_visual_bboxes.append(bbox)
                warnings.append(f"detected_{visual['object_family']}_as_logical_visual")

            # Preserve positioned native words inside analytical visual regions.
            # They are a second evidence channel, not inferred facts, and recover
            # labels that raster OCR can miss (notably rotated bar values).
            for region in regions:
                if region.kind != "visual":
                    continue
                bbox = tuple(region.metadata.get("source_bbox_points") or ())
                if len(bbox) != 4:
                    continue
                native_visual_words = []
                for word in positioned_words:
                    word_bbox = (
                        float(word["x0"]), float(word["top"]),
                        float(word["x1"]), float(word["bottom"]),
                    )
                    if not _center_in(word_bbox, bbox):
                        continue
                    native_visual_words.append({
                        "text": str(word.get("text", "")),
                        "coordinates": _normalized_xywh(word_bbox, width, height),
                        "confidence": 1.0,
                        "evidence_source": "native_pdf_positioned_word",
                    })
                region.metadata["native_visual_words"] = native_visual_words

            visual_bboxes = [tuple(region.metadata["source_bbox_points"]) for region in regions if region.kind == "visual"]
            excluded = table_bboxes + visual_bboxes
            words = page.extract_words(extra_attrs=["size"], use_text_flow=True) or []
            words = [word for word in words if not any(_center_in(
                (float(word["x0"]), float(word["top"]), float(word["x1"]), float(word["bottom"])), bbox
            ) for bbox in excluded)]
            for paragraph in _group_words(words):
                order += 1
                regions.append(Region(
                    region_id=f"p{page_number:03d}-r{order:03d}", page=page_number,
                    kind="normal_text", coordinates=_normalized_xywh(paragraph["bbox"], width, height),
                    reading_order=order, classification_method="native-positioned-words",
                    confidence=quality, native_text=paragraph["text"],
                    metadata={
                        "median_font_size": paragraph["median_font_size"],
                        "word_count": paragraph["word_count"],
                        "source_bbox_points": list(paragraph["bbox"]),
                    },
                ))

            if not regions:
                regions.append(Region(
                    region_id=f"p{page_number:03d}-r001", page=page_number,
                    kind="unknown", coordinates=[0.0, 0.0, 1000.0, 1000.0], reading_order=1,
                    classification_method="inspection-fallback", confidence=0.25,
                ))
            left_text = [
                region for region in regions
                if region.kind == "normal_text" and region.coordinates[0] + region.coordinates[2] <= 510
            ]
            right_text = [
                region for region in regions
                if region.kind == "normal_text" and region.coordinates[0] >= 490
            ]
            if len(left_text) >= 2 and len(right_text) >= 2:
                # Complete the left reading lane before moving to the right lane.
                # This prevents legal disclosures and other two-column prose from
                # being interleaved merely because their baselines line up.
                regions.sort(key=lambda region: (
                    3 if (region.kind == "decoration" or (
                        region.coordinates[1] >= 820 and region.coordinates[2] >= 700
                    )) else
                    1 if region in right_text else 0,
                    region.coordinates[1], region.coordinates[0], region.reading_order,
                ))
                warnings.append("two_column_reading_order_preserved")
            else:
                regions.sort(key=lambda region: (region.coordinates[1], region.coordinates[0], region.reading_order))
            for index, region in enumerate(regions, 1):
                region.reading_order = index

            ambiguous_visuals = sum(region.kind in {"visual", "unknown"} for region in regions)
            routing_confidence = min(
                [region.confidence for region in regions] or [0.0]
            ) if ambiguous_visuals else max(0.70, quality)
            inspections.append(PageInspection(
                page=page_number, width_points=width, height_points=height,
                rotation=int(page.rotation or 0), native_text_available=bool(chars),
                native_text_coverage=coverage, native_text_quality=quality,
                image_coverage=round(image_coverage, 5), vector_line_count=vector_count,
                possible_table_regions=len(table_bboxes), possible_visual_regions=ambiguous_visuals,
                routing_confidence=round(routing_confidence, 5), regions=regions, warnings=warnings,
            ))
    mark_repeated_decorations(inspections)
    return inspections, metadata

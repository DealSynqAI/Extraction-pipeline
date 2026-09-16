"""Score frozen, visibly checked content anchors in two Coulton extraction runs.

This is a development-set content comparison, not a blinded accuracy estimate.
The scorer deliberately counts *owned* table cells/chart labels/map states, not
numbers merely mentioned somewhere on a page. It also records failures and
separate structural checks. See reference.json for every scored anchor.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from jsonschema import Draft202012Validator

from run_vision_only import SCHEMA


def norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def value_number(value: object) -> Decimal | None:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def same_value(left: object, right: object) -> bool:
    a, b = value_number(left), value_number(right)
    if a is None or b is None:
        return norm(left) == norm(right)
    return a == b and ("%" in str(left)) == ("%" in str(right))


def find_blocks(page: dict, kind: str, method: str) -> list[dict]:
    return [block for block in page.get("blocks", []) if block.get("type") == kind]


def table_cells(page: dict, method: str) -> list[tuple[str, str, str]]:
    out = []
    for block in find_blocks(page, "table", method):
        if method == "vision":
            for row in block.get("rows", []):
                for cell in row.get("cells", []):
                    out.append((row.get("label", ""), cell.get("column", ""), cell.get("value", "")))
        else:
            columns = {col["column_id"]: col["label"] for col in block["content"]["columns"]}
            for row in block["content"]["rows"]:
                for cell in row["cells"]:
                    out.append((row["label"], columns[cell["column_id"]], cell.get("raw_value", "")))
    return out


def same_table_cell(left: str, right: str) -> bool:
    # Empty printed cells and printed dashes are *different*. Numeric/currency
    # spacing is formatting-only and does not affect cell ownership.
    if not str(left).strip() or not str(right).strip():
        return not str(left).strip() and not str(right).strip()
    if str(left).strip() == "-" or str(right).strip() == "-":
        return str(left).strip() == str(right).strip()
    return same_value(left, right)


def table_agreement(full_page: dict, vision_page: dict) -> dict:
    full = table_cells(full_page, "full")
    vision = table_cells(vision_page, "vision")
    by_owner = {(norm(row), norm(col)): value for row, col, value in vision}
    matched = 0
    different = []
    for row, col, expected in full:
        actual = by_owner.get((norm(row), norm(col)))
        if actual is not None and same_table_cell(expected, actual):
            matched += 1
        else:
            different.append({"row": row, "column": col, "full": expected, "vision": actual})
    return {"full_cells": len(full), "vision_cells": len(vision),
            "agreement": matched, "differences": different}


def chart_bindings(page: dict, method: str) -> list[tuple[str, str]]:
    out = []
    for block in find_blocks(page, "chart", method):
        if method == "vision":
            out += [(item.get("label", ""), item.get("value", "")) for item in block.get("items", [])]
        else:
            out += [(item.get("category", ""), item.get("raw_value", ""))
                    for item in block.get("content", {}).get("observations", [])]
    return out


def map_bindings(page: dict, method: str) -> list[tuple[str, str]]:
    out = []
    for block in page.get("blocks", []):
        if method == "vision":
            if block.get("type") == "map":
                out += [(item.get("label", ""), item.get("value", ""))
                        for item in block.get("items", [])]
            # Count substantive map assignments even if the model misclassified
            # the parent as a chart. That parent-type error is reported apart.
            out += [(row.get("label", ""), cell.get("value", ""))
                    for row in block.get("rows", []) if norm(row.get("section")) == "map"
                    for cell in row.get("cells", []) if norm(cell.get("column")) == "value"]
        elif block.get("type") == "map":
            out += [(item.get("geography", ""), item.get("raw_value", ""))
                    for item in block.get("content", {}).get("bindings", [])]
    return out


def map_agreement(full_page: dict, vision_page: dict) -> dict:
    full = map_bindings(full_page, "full")
    vision = map_bindings(vision_page, "vision")
    by_place = {norm(place): value for place, value in full}
    matched = [(place, value) for place, value in vision
               if norm(place) in by_place and same_value(value, by_place[norm(place)])]
    names = Counter(norm(place) for place, _ in vision)
    return {"full_bindings": len(full), "vision_bindings": len(vision),
            "exact_agreement": len(matched),
            "repeated_places": {place: count for place, count in names.items() if count > 1},
            "vision_parent_types": [block.get("type") for block in vision_page.get("blocks", [])]}


def chart_agreement(full_page: dict, vision_page: dict) -> dict:
    full = chart_bindings(full_page, "full")
    vision = chart_bindings(vision_page, "vision")
    used = set()
    matched = 0
    for label, value in full:
        for index, (candidate_label, candidate_value) in enumerate(vision):
            if index not in used and norm(label) == norm(candidate_label) and same_value(value, candidate_value):
                used.add(index)
                matched += 1
                break
    return {"full_observations": len(full), "vision_items": len(vision), "exact_agreement": matched}


def matched_pair(bindings: list[tuple[str, str]], label: str, value: str) -> bool:
    owners = [observed for found_label, observed in bindings if norm(found_label) == norm(label)]
    return len(owners) == 1 and same_value(owners[0], value)


def kpi_binding(page: dict, method: str, anchor: dict) -> bool:
    for block in find_blocks(page, "chart", method):
        items = block.get("items", []) if method == "vision" else block.get("content", {}).get("observations", [])
        for item in items:
            label = item.get("label", "") if method == "vision" else item.get("category", "")
            value = item.get("value", "") if method == "vision" else item.get("raw_value", "")
            unit = item.get("unit", "")
            # The complete caption must belong to the item, not merely appear
            # elsewhere in the block or its OCR ledger.
            if norm(label) != norm(anchor["label"]) or value_number(value) != value_number(anchor["value"]):
                continue
            if "million" in anchor["unit"] and "million" not in norm(str(value) + str(unit)):
                continue
            if anchor["unit"] == "investments" and "investments" not in norm(str(unit)):
                continue
            return True
    return False


def paired_pie(page: dict, method: str, anchor: dict) -> bool:
    label = norm(anchor["label"])
    for block in find_blocks(page, "chart", method):
        if method == "vision":
            candidates = []
            for item in block.get("items", []):
                if norm(item.get("label")) == label:
                    candidates.append(str(item.get("value", "")))
            for row in block.get("rows", []):
                if norm(row.get("label")) == label:
                    candidates += [str(cell.get("value", "")) for cell in row.get("cells", [])]
            amount = any("$" in value and value_number(value) == value_number(anchor["amount"])
                         for value in candidates)
            percent = any("%" in value and value_number(value) == value_number(anchor["percent"])
                          for value in candidates)
            if amount and percent:
                return True
        else:
            candidates = [str(item.get("raw_value", ""))
                          for item in block.get("content", {}).get("observations", [])
                          if norm(item.get("category")) == label]
            if any("$" in value and value_number(value) == value_number(anchor["amount"])
                   for value in candidates) and any(
                       "%" in value and value_number(value) == value_number(anchor["percent"])
                       for value in candidates):
                return True
    return False


def panel_claims(page: dict, method: str) -> list[tuple[str, str]]:
    out = []
    for block in page.get("blocks", []):
        if method == "vision":
            out += [(item.get("parent", ""), item.get("value", ""))
                    for item in block.get("items", [])]
        elif block.get("type") == "comparison_panel":
            for section in block.get("content", {}).get("sections", []):
                for sub in section.get("subsections", []):
                    out += [(sub.get("title", ""), claim.get("text", ""))
                            for claim in sub.get("claims", [])]
                out += [(section.get("title", ""), claim.get("text", ""))
                        for claim in section.get("claims", [])]
    return out


def text_blocks(page: dict, method: str) -> list[str]:
    return [str(block.get("text", "") if method == "vision" else block.get("content", {}).get("text", ""))
            for block in page.get("blocks", []) if block.get("type") == "text"]


def page_fact(page: dict, method: str, phrase: str) -> bool:
    strings = text_blocks(page, method)
    normalized = norm(phrase)
    if "126" in normalized:
        return any(all(token in norm(text) for token in ("126", "105", "realized")) for text in strings)
    if "average" in normalized:
        return any(all(token in norm(text) for token in ("average", "180", "irr")) for text in strings)
    if "median" in normalized:
        return any(all(token in norm(text) for token in ("median", "151", "irr")) for text in strings)
    return any(normalized in norm(text) for text in strings)


def legal_clause(page: dict, method: str, phrase: str) -> bool:
    # Permit one phrase to cross *adjacent* source blocks, but not to be
    # interrupted by a different column's sentence.
    texts = text_blocks(page, method)
    expected = norm(phrase)
    return any(expected in norm(text) or expected in norm(text + " " + next_text)
               for text, next_text in zip(texts, texts[1:] + [""]))


def column_order(page: dict, method: str) -> bool:
    text = norm(" ".join(text_blocks(page, method)))
    terms = ["thisisneitheranoffer", "nolegallybindingobligations", "confidentialandproprietary",
             "totakereasonablesteps", "haselectedtobetaxedasapartnership", "pastperformanceisnoguarantee"]
    positions = [text.find(term) for term in terms]
    return all(position >= 0 for position in positions) and positions == sorted(positions)


def score_anchor(page: dict, method: str, anchor: dict) -> bool:
    kind = anchor["kind"]
    if kind == "table_cell":
        return matched_pair([(row + "|" + col, value) for row, col, value in table_cells(page, method)],
                            anchor["row"] + "|" + anchor["column"], anchor["value"])
    if kind == "chart_binding":
        return matched_pair(chart_bindings(page, method), anchor["label"], anchor["value"])
    if kind == "map_binding":
        return matched_pair(map_bindings(page, method), anchor["label"], anchor["value"])
    if kind == "kpi_binding":
        return kpi_binding(page, method, anchor)
    if kind == "paired_pie_binding":
        return paired_pie(page, method, anchor)
    if kind == "panel_claim":
        return any(norm(owner) == norm(anchor["owner"]) and norm(anchor["phrase"]) in norm(text)
                   for owner, text in panel_claims(page, method))
    if kind == "page_fact":
        return page_fact(page, method, anchor["phrase"])
    if kind == "chart_type":
        return any(anchor["value"] in norm(str(block.get("title", "")) + " " + str(block.get("text", "")))
                   if method == "vision" else norm(block.get("content", {}).get("chart_type", "")) == norm(anchor["value"])
                   for block in find_blocks(page, "chart", method))
    if kind == "chart_title":
        return any(norm(anchor["phrase"]) in norm(block.get("title", "") if method == "vision"
                                                  else block.get("content", {}).get("title", ""))
                   for block in find_blocks(page, "chart", method))
    if kind == "safe_scatter":
        if not any(str(anchor["point_claim"]) in text for text in text_blocks(page, method)):
            return False
        if method == "vision":
            return not any(value_number(item.get("value")) == Decimal("100") and "%" in str(item.get("value"))
                           for block in find_blocks(page, "chart", method) for item in block.get("items", []))
        return not any(value_number(item.get("raw_value")) == Decimal("100")
                       and "%" in str(item.get("raw_value"))
                       for block in find_blocks(page, "chart", method)
                       for item in block.get("content", {}).get("observations", []))
    if kind == "legal_clause":
        return legal_clause(page, method, anchor["phrase"])
    if kind == "column_order":
        return column_order(page, method)
    raise ValueError(kind)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--vision", type=Path, required=True)
    parser.add_argument("--full", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text(encoding="utf-8"))
    results = {"vision": {}, "full": {}}
    for page_number in reference["scored_pages"]:
        key = str(page_number)
        anchors = reference["anchors"][key]
        for method, root in (("vision", args.vision), ("full", args.full)):
            page_file = root / f"page-{page_number:03d}.json"
            page = json.loads(page_file.read_text(encoding="utf-8"))
            findings = []
            for index, anchor in enumerate(anchors, 1):
                findings.append({"anchor_id": f"p{page_number:03d}-a{index:02d}", "anchor": anchor,
                                 "correct": score_anchor(page, method, anchor)})
            results[method][key] = {"correct": sum(item["correct"] for item in findings),
                                    "total": len(findings), "findings": findings}

    vision_receipts = []
    for page_number in range(1, 17):
        receipt_file = args.vision / f"page-{page_number:03d}.receipt.json"
        vision_receipts.append(json.loads(receipt_file.read_text(encoding="utf-8")))
    schema_issues = {}
    for page_number in range(1, 17):
        page_file = args.vision / f"page-{page_number:03d}.json"
        if not page_file.exists():
            schema_issues[str(page_number)] = ["No parseable page JSON"]
            continue
        page = json.loads(page_file.read_text(encoding="utf-8"))
        schema_issues[str(page_number)] = [error.message for error in Draft202012Validator(SCHEMA).iter_errors(page)]
    full_manifest = json.loads((args.full.parent / "manifest.json").read_text(encoding="utf-8"))
    start = datetime.fromisoformat(full_manifest["created_utc"])
    end = datetime.fromisoformat(full_manifest["finished_utc"])
    summary = {
        "schema_version": "qwen3vl-vision-only-vs-full-benchmark/1.0",
        "reference_status": reference["reference_status"],
        "source_sha256": reference["source_sha256"],
        "scored_pages": reference["scored_pages"],
        "anchors_scored": sum(len(items) for items in reference["anchors"].values()),
        "scores": results,
        "totals": {method: {"correct": sum(item["correct"] for item in pages.values()),
                            "total": sum(item["total"] for item in pages.values())}
                   for method, pages in results.items()},
        "vision_page_attempts": Counter(receipt["status"] for receipt in vision_receipts),
        "vision_seconds_total": round(sum(float(receipt["seconds"]) for receipt in vision_receipts), 3),
        "vision_seconds_by_page": {str(receipt["page"]): receipt["seconds"] for receipt in vision_receipts},
        "vision_json_schema_issue_count": sum(bool(issues) for issues in schema_issues.values()),
        "vision_json_schema_issues_by_page": {page: issues for page, issues in schema_issues.items() if issues},
        "full_seconds_total": round((end - start).total_seconds(), 3),
        "full_pages_complete": len(full_manifest["selected_pages"]),
        "full_schema_valid": full_manifest["validation"]["valid"],
        "secondary_full_output_agreement_not_gold": {
            "page_006_table": table_agreement(
                json.loads((args.full / "page-006.json").read_text(encoding="utf-8")),
                json.loads((args.vision / "page-006.json").read_text(encoding="utf-8")),
            ),
            "page_008_map": map_agreement(
                json.loads((args.full / "page-008.json").read_text(encoding="utf-8")),
                json.loads((args.vision / "page-008.json").read_text(encoding="utf-8")),
            ),
            "bar_charts": {str(page): chart_agreement(
                json.loads((args.full / f"page-{page:03d}.json").read_text(encoding="utf-8")),
                json.loads((args.vision / f"page-{page:03d}.json").read_text(encoding="utf-8")),
            ) for page in (11, 13, 14)},
        },
        "important_limitations": [
            "The reference is visually checked development evidence from one PDF, not a blind multi-document human gold set.",
            "Vision-only used a lighter semantic-page JSON contract; full output used Unified Source Blocks with coordinates and evidence hashes. Content anchors, not schema fields, are compared head-to-head.",
            "Runtime is illustrative: the full run and vision-only calls were recorded in separate sessions, not randomized paired hardware trials.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=list) + "\n", encoding="utf-8")
    print(json.dumps({"totals": summary["totals"],
                      "vision_page_attempts": dict(summary["vision_page_attempts"]),
                      "vision_seconds_total": summary["vision_seconds_total"],
                      "full_seconds_total": summary["full_seconds_total"]}, indent=2))


if __name__ == "__main__":
    main()

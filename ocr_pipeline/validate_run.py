from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_new(path: Path, payload: Any) -> None:
    if path.exists():
        raise FileExistsError(f"Validation report already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _resolve_run_path(run: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else run / path


def validate_run(run: Path, schema_path: Path | None = None) -> dict[str, Any]:
    run = run.resolve(strict=True)
    schema_path = (schema_path or Path(__file__).resolve().parents[1] / "schemas/unified-source-block.schema.json").resolve(strict=True)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    run_manifest_path = run / "manifest.json"
    collection_path = run / "source-blocks/document-manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    schema_errors: list[dict[str, Any]] = []
    integrity_errors: list[str] = []
    status_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    review_by_page: Counter[int] = Counter()
    unassigned_by_page: dict[str, int] = {}
    total_blocks = 0

    if run_manifest.get("status") != "complete":
        integrity_errors.append(f"run status is {run_manifest.get('status')!r}, not 'complete'")
    if run_manifest.get("schema_version") != "unified-source-collection/1.1":
        integrity_errors.append("run manifest is not unified-source-collection/1.1")
    if collection.get("schema_version") != "unified-source-collection/1.1":
        integrity_errors.append("collection manifest is not unified-source-collection/1.1")
    if collection.get("semantic_interpretation_performed") is not False:
        integrity_errors.append("collection does not explicitly stop before semantic interpretation")
    if run_manifest.get("source_sha256") != collection.get("source_sha256"):
        integrity_errors.append("run and collection source hashes differ")

    inspection_ref = run_manifest.get("inspection", {})
    inspection_path = run / str(inspection_ref.get("path", ""))
    if not inspection_path.is_file():
        integrity_errors.append("separate inspection file is missing")
        inspection = {}
    else:
        inspection = json.loads(inspection_path.read_text(encoding="utf-8"))
        if _sha256(inspection_path) != inspection_ref.get("sha256"):
            integrity_errors.append("inspection file hash mismatch")
        if inspection.get("schema_version") != "pdf-inspection/1.0":
            integrity_errors.append("inspection schema is not pdf-inspection/1.0")
        if inspection.get("document_id") != collection.get("document_id"):
            integrity_errors.append("inspection document_id mismatch")
        if inspection.get("source_sha256") != collection.get("source_sha256"):
            integrity_errors.append("inspection source hash mismatch")
        if inspection.get("page_count") != collection.get("page_count"):
            integrity_errors.append("inspection page_count mismatch")
        if inspection.get("selected_pages") != collection.get("selected_pages"):
            integrity_errors.append("inspection selected_pages mismatch")
        inspected_pages = [int(page.get("page")) for page in inspection.get("pages", [])]
        if inspected_pages != collection.get("selected_pages"):
            integrity_errors.append("inspection page records do not match selected pages")
    preserved_source = _resolve_run_path(run, run_manifest.get("preserved_source"))
    if not preserved_source.is_file():
        integrity_errors.append("preserved source file is missing")
    elif _sha256(preserved_source) != collection.get("source_sha256"):
        integrity_errors.append("preserved source hash differs from collection source hash")

    expected_pages = list(collection.get("selected_pages", []))
    listed_pages = [int(record["page"]) for record in collection.get("pages", [])]
    if listed_pages != expected_pages:
        integrity_errors.append(f"page listing {listed_pages} does not match selected pages {expected_pages}")

    for page_record in collection.get("pages", []):
        page_number = int(page_record["page"])
        page_path = run / "source-blocks" / page_record["file"]
        if not page_path.is_file():
            integrity_errors.append(f"page {page_number}: missing {page_record['file']}")
            continue
        actual_hash = _sha256(page_path)
        if actual_hash != page_record.get("sha256"):
            integrity_errors.append(f"page {page_number}: page JSON hash mismatch")
        page_payload = json.loads(page_path.read_text(encoding="utf-8"))
        if page_payload.get("schema_version") != "unified-source-page/2.0":
            integrity_errors.append(f"page {page_number}: page schema is not unified-source-page/2.0")
        if "inspection" in page_payload:
            integrity_errors.append(f"page {page_number}: v2 page JSON embeds inspection")
        if page_payload.get("page") != page_number:
            integrity_errors.append(f"page {page_number}: page number mismatch inside JSON")
        if page_payload.get("document_id") != collection.get("document_id"):
            integrity_errors.append(f"page {page_number}: document_id mismatch")
        if page_payload.get("source_sha256") != collection.get("source_sha256"):
            integrity_errors.append(f"page {page_number}: source hash mismatch")
        rendered = _resolve_run_path(run, page_payload.get("evidence_ledger", {}).get("rendered_page"))
        rendered_hash = page_payload.get("evidence_ledger", {}).get("rendered_page_sha256")
        if not rendered.is_file():
            integrity_errors.append(f"page {page_number}: rendered evidence image is missing")
        elif _sha256(rendered) != rendered_hash:
            integrity_errors.append(f"page {page_number}: rendered evidence hash mismatch")

        blocks = page_payload.get("blocks", [])
        total_blocks += len(blocks)
        known_ids = {block.get("block_id") for block in blocks}
        blocks_by_id = {block.get("block_id"): block for block in blocks}
        required_parent_types = {
            "chart_observation": "chart",
            "map_binding": "map",
            "table_header": "table",
            "table_record": "table",
        }
        if len(known_ids) != len(blocks):
            integrity_errors.append(f"page {page_number}: block IDs are not unique")
        for block_index, block in enumerate(blocks):
            for error in validator.iter_errors(block):
                schema_errors.append({
                    "page": page_number,
                    "block_id": block.get("block_id"),
                    "block_index": block_index,
                    "json_path": "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path),
                    "message": error.message,
                })
            parent = block.get("parent_block_id")
            if parent is not None and parent not in known_ids:
                integrity_errors.append(f"page {page_number}: {block.get('block_id')} has missing parent {parent}")
            expected_parent_type = required_parent_types.get(block.get("type"))
            if expected_parent_type and parent is None:
                integrity_errors.append(
                    f"page {page_number}: {block.get('block_id')} requires a {expected_parent_type} parent"
                )
            elif expected_parent_type and parent in blocks_by_id and blocks_by_id[parent].get("type") != expected_parent_type:
                integrity_errors.append(
                    f"page {page_number}: {block.get('block_id')} parent is not {expected_parent_type}"
                )
            if block.get("page") != page_number:
                integrity_errors.append(f"page {page_number}: {block.get('block_id')} has incorrect page field")
            if block.get("document_id") != collection.get("document_id"):
                integrity_errors.append(f"page {page_number}: {block.get('block_id')} has incorrect document_id")
            content = block.get("content", {})
            if "vision_features" in content or "line_segments" in content:
                integrity_errors.append(f"page {page_number}: {block.get('block_id')} embeds raw vision features")
            if block.get("type") in {"chart", "map", "photograph", "decoration", "unclassified_visual"} and parent is None:
                feature_ref = content.get("vision_features_ref")
                if not isinstance(feature_ref, dict) or not feature_ref.get("path") or not feature_ref.get("sha256"):
                    integrity_errors.append(f"page {page_number}: {block.get('block_id')} has no complete vision_features_ref")
                else:
                    diagnostic_path = run / str(feature_ref["path"])
                    if not diagnostic_path.is_file():
                        integrity_errors.append(f"page {page_number}: {block.get('block_id')} vision diagnostic is missing")
                    else:
                        if _sha256(diagnostic_path) != feature_ref["sha256"]:
                            integrity_errors.append(f"page {page_number}: {block.get('block_id')} vision diagnostic hash mismatch")
                        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
                        if diagnostic.get("schema_version") != "opencv-region-diagnostic/1.0":
                            integrity_errors.append(f"page {page_number}: {block.get('block_id')} vision diagnostic schema mismatch")
                        if diagnostic.get("document_id") != collection.get("document_id"):
                            integrity_errors.append(f"page {page_number}: {block.get('block_id')} diagnostic document_id mismatch")
                        if diagnostic.get("source_sha256") != collection.get("source_sha256"):
                            integrity_errors.append(f"page {page_number}: {block.get('block_id')} diagnostic source hash mismatch")
                        if diagnostic.get("page") != page_number:
                            integrity_errors.append(f"page {page_number}: {block.get('block_id')} diagnostic page mismatch")
                        if diagnostic.get("region_id") != block.get("provenance", {}).get("region_id"):
                            integrity_errors.append(f"page {page_number}: {block.get('block_id')} diagnostic region mismatch")
            status = str(block.get("validation", {}).get("status"))
            status_counts[status] += 1
            type_counts[str(block.get("type"))] += 1
            if status == "needs_review":
                review_by_page[page_number] += 1
        child_counts = Counter(block.get("parent_block_id") for block in blocks if block.get("parent_block_id"))
        for block in blocks:
            if block.get("type") != "chart":
                continue
            expected = block.get("content", {}).get("expected_observation_count")
            emitted = block.get("content", {}).get("emitted_observation_count")
            actual = child_counts.get(block.get("block_id"), 0)
            if isinstance(emitted, int) and emitted != actual:
                integrity_errors.append(
                    f"page {page_number}: {block.get('block_id')} emitted count {emitted} differs from {actual} children"
                )
            if isinstance(expected, int) and expected > 0 and actual != expected and block.get("validation", {}).get("status") == "passed":
                integrity_errors.append(
                    f"page {page_number}: incomplete chart {block.get('block_id')} is marked passed"
                )
        unassigned_by_page[str(page_number)] = len(page_payload.get("unassigned_evidence", []))

    if total_blocks != sum(int(value) for value in collection.get("block_counts_by_type", {}).values()):
        integrity_errors.append("total blocks do not match collection type counts")
    if dict(sorted(type_counts.items())) != collection.get("block_counts_by_type"):
        integrity_errors.append("recomputed block type counts do not match collection manifest")
    if dict(sorted(status_counts.items())) != collection.get("validation_counts"):
        integrity_errors.append("recomputed validation counts do not match collection manifest")

    return {
        "schema_version": "unified-source-collection-validation/1.1",
        "run": str(run),
        "source_sha256": collection.get("source_sha256"),
        "schema": str(schema_path),
        "schema_sha256": _sha256(schema_path),
        "json_schema_draft": "2020-12",
        "validator": "jsonschema.Draft202012Validator",
        "pages_validated": len(listed_pages),
        "blocks_validated": total_blocks,
        "schema_valid": not schema_errors,
        "schema_error_count": len(schema_errors),
        "schema_errors": schema_errors,
        "integrity_valid": not integrity_errors,
        "integrity_error_count": len(integrity_errors),
        "integrity_errors": integrity_errors,
        "block_counts_by_type": dict(sorted(type_counts.items())),
        "validation_counts": dict(sorted(status_counts.items())),
        "needs_review_by_page": {str(key): value for key, value in sorted(review_by_page.items())},
        "unassigned_evidence_by_page": unassigned_by_page,
        "content_approval_claimed": False,
        "result": "valid" if not schema_errors and not integrity_errors else "invalid",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an immutable Unified Source Block run")
    parser.add_argument("run", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = validate_run(args.run, args.schema)
    if args.output:
        _write_json_new(args.output.resolve(), report)
        print(args.output.resolve())
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["result"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())

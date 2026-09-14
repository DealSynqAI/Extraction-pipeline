from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ValidationStatus = Literal["passed", "needs_review", "failed"]


@dataclass
class Region:
    region_id: str
    page: int
    kind: str
    coordinates: list[float]
    reading_order: int
    classification_method: str
    confidence: float
    native_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageInspection:
    page: int
    width_points: float
    height_points: float
    rotation: int
    native_text_available: bool
    native_text_coverage: float
    native_text_quality: float
    image_coverage: float
    vector_line_count: int
    possible_table_regions: int
    possible_visual_regions: int
    routing_confidence: float
    regions: list[Region]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["regions"] = [region.as_dict() for region in self.regions]
        return payload


@dataclass
class SourceBlock:
    document_id: str
    type: str
    page: int
    block_id: str
    parent_block_id: str | None
    content: dict[str, Any]
    coordinates: list[float]
    extraction_method: list[str]
    confidence: float
    validation_status: ValidationStatus
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "unified-source-block/1.0",
            "document_id": self.document_id,
            "type": self.type,
            "page": self.page,
            "block_id": self.block_id,
            "parent_block_id": self.parent_block_id,
            "content": self.content,
            "coordinates": [round(float(value), 6) for value in self.coordinates],
            "coordinate_system": "top-left normalized xywh 0..1000",
            "extraction_method": self.extraction_method,
            "confidence": round(max(0.0, min(1.0, float(self.confidence))), 5),
            "validation": {
                "status": self.validation_status,
                "errors": self.errors,
                "warnings": self.warnings,
            },
            "provenance": self.provenance,
        }


def validate_source_blocks(blocks: list[dict[str, Any]]) -> list[str]:
    """Validate the common contract and page-local parent links."""
    errors: list[str] = []
    ids = [str(block.get("block_id", "")) for block in blocks]
    known = set(ids)
    by_id = {str(block.get("block_id", "")): block for block in blocks}
    required_parent_types = {
        "chart_observation": "chart",
        "map_binding": "map",
        "table_header": "table",
        "table_record": "table",
    }
    if "" in known:
        errors.append("Every block must have a non-empty block_id")
    if len(ids) != len(known):
        errors.append("Block IDs must be unique within a page")
    required = {
        "schema_version", "document_id", "type", "page", "block_id",
        "parent_block_id", "content", "coordinates", "coordinate_system",
        "extraction_method", "confidence", "validation", "provenance",
    }
    for block in blocks:
        block_id = block.get("block_id", "<missing>")
        missing = sorted(required - set(block))
        if missing:
            errors.append(f"{block_id}: missing fields {missing}")
        coordinates = block.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) != 4:
            errors.append(f"{block_id}: coordinates must be normalized xywh")
        elif any(not isinstance(value, (int, float)) or value < 0 or value > 1000 for value in coordinates):
            errors.append(f"{block_id}: coordinates must be within 0..1000")
        confidence = block.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"{block_id}: confidence must be within 0..1")
        validation = block.get("validation", {})
        if validation.get("status") not in {"passed", "needs_review", "failed"}:
            errors.append(f"{block_id}: invalid validation status")
        parent = block.get("parent_block_id")
        if parent is not None and parent not in known:
            errors.append(f"{block_id}: unknown parent {parent}")
        expected_parent_type = required_parent_types.get(str(block.get("type")))
        if expected_parent_type and parent is None:
            errors.append(f"{block_id}: {block.get('type')} requires a {expected_parent_type} parent")
        elif expected_parent_type and parent in by_id and by_id[parent].get("type") != expected_parent_type:
            errors.append(
                f"{block_id}: {block.get('type')} parent must be {expected_parent_type}, "
                f"not {by_id[parent].get('type')}"
            )
        if not isinstance(block.get("content"), dict):
            errors.append(f"{block_id}: content must be an object")
        elif "vision_features" in block["content"] or "line_segments" in block["content"]:
            errors.append(f"{block_id}: raw vision features must be stored in diagnostics")
    return errors

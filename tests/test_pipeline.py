from __future__ import annotations

import unittest
from pathlib import Path
import json
import tempfile

from ocr_pipeline import __version__
from ocr_pipeline.inspection import (
    detect_aligned_financial_table,
    detect_small_raster_chart,
    detect_vector_bar_chart,
    mark_repeated_decorations,
    merge_visual_objects,
    _merge_numeric_fragments,
)
from ocr_pipeline.models import PageInspection, Region, SourceBlock, validate_source_blocks
from ocr_pipeline.workers import normalize_vision_payload
from ocr_pipeline.pipeline import (
    COLLECTION_SCHEMA_VERSION,
    INSPECTION_SCHEMA_VERSION,
    PAGE_SCHEMA_VERSION,
    PIPELINE_VERSION,
    VISION_DIAGNOSTIC_SCHEMA_VERSION,
    _bar_bindings,
    _table_total_reconciliation,
    _infer_chart_type,
    _numeric_value,
    _proximity_bindings,
    _visual_title,
    _vision_summary,
    _write_vision_diagnostic,
    clean_text,
    parse_pages,
)


class PipelineUnitTests(unittest.TestCase):
    def test_v040_versions(self) -> None:
        self.assertEqual(__version__, "0.4.0")
        self.assertEqual(PIPELINE_VERSION, "0.4.0")
        self.assertEqual(PAGE_SCHEMA_VERSION, "unified-source-page/2.0")
        self.assertEqual(COLLECTION_SCHEMA_VERSION, "unified-source-collection/1.1")
        self.assertEqual(INSPECTION_SCHEMA_VERSION, "pdf-inspection/1.0")
        self.assertEqual(VISION_DIAGNOSTIC_SCHEMA_VERSION, "opencv-region-diagnostic/1.0")

    def test_page_parser(self) -> None:
        self.assertEqual(parse_pages("1-3,5,3", 6), [1, 2, 3, 5])
        with self.assertRaises(ValueError):
            parse_pages("7", 6)

    def test_ocr_cleanup_dehyphenates_and_joins(self) -> None:
        raw = "The firm tar-\ngets income produc-\ning real estate.\n\nNext paragraph."
        self.assertEqual(
            clean_text(raw),
            "The firm targets income producing real estate.\n\nNext paragraph.",
        )

    def test_common_contract_and_parent_link(self) -> None:
        parent = SourceBlock(
            document_id="doc", type="table", page=1, block_id="p1-table", parent_block_id=None,
            content={"rows": []}, coordinates=[0, 0, 1000, 1000],
            extraction_method=["test"], confidence=0.8, validation_status="passed",
            provenance={"source_sha256": "0" * 64, "region_id": "r1"},
        ).as_dict()
        child = SourceBlock(
            document_id="doc", type="table_record", page=1, block_id="p1-cell", parent_block_id="p1-table",
            content={"row": "A", "column": "B", "value": "1"}, coordinates=[1, 1, 10, 10],
            extraction_method=["test"], confidence=0.8, validation_status="passed",
            provenance={"source_sha256": "0" * 64, "region_id": "r1"},
        ).as_dict()
        self.assertEqual(validate_source_blocks([parent, child]), [])

    def test_common_contract_rejects_embedded_raw_vision(self) -> None:
        block = SourceBlock(
            document_id="doc", type="chart", page=1, block_id="chart", parent_block_id=None,
            content={"vision_features": {"line_segments": []}}, coordinates=[0, 0, 10, 10],
            extraction_method=["test"], confidence=0.8, validation_status="passed",
            provenance={"source_sha256": "0" * 64, "region_id": "r1"},
        ).as_dict()
        self.assertIn("raw vision features", " ".join(validate_source_blocks([block])))

    def test_child_requires_correct_parent_type(self) -> None:
        wrong_parent = SourceBlock(
            document_id="doc", type="text", page=1, block_id="text", parent_block_id=None,
            content={"text": "wrong"}, coordinates=[0, 0, 10, 10], extraction_method=["test"],
            confidence=0.8, validation_status="passed",
        ).as_dict()
        child = SourceBlock(
            document_id="doc", type="chart_observation", page=1, block_id="obs", parent_block_id="text",
            content={"value": "1"}, coordinates=[0, 0, 10, 10], extraction_method=["test"],
            confidence=0.8, validation_status="passed",
        ).as_dict()
        self.assertIn("parent must be chart", " ".join(validate_source_blocks([wrong_parent, child])))

    def test_vision_summary_contains_counts_not_arrays(self) -> None:
        summary = _vision_summary({
            "horizontal_lines": 4, "vertical_lines": 2, "bar_candidates": 3,
            "line_segments": {"horizontal": [[0, 0, 1, 1]]},
        })
        self.assertEqual(summary["horizontal_line_count"], 4)
        self.assertEqual(summary["bar_candidate_count"], 3)
        self.assertNotIn("line_segments", summary)

    def test_vision_payload_is_normalized_to_routing_contract(self) -> None:
        payload = normalize_vision_payload({
            "type": " Chart ", "confidence": "1.4", "chart_type": " BAR ",
            "bindings": [{"label": "A", "value": "1"}, "invalid"], "extra": "discarded",
        })
        self.assertEqual(payload["type"], "chart")
        self.assertEqual(payload["confidence"], 1.0)
        self.assertEqual(payload["chart_type"], "bar")
        self.assertEqual(payload["bindings"], [{"label": "A", "value": "1"}])
        self.assertNotIn("extra", payload)

    def test_diagnostic_explains_coordinate_formats_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            diagnostics = run / "diagnostics" / "vision-features"
            diagnostics.mkdir(parents=True)
            region = Region("p001-r001", 1, "visual", [10, 20, 30, 40], 1, "test", 0.8)
            ref = _write_vision_diagnostic(
                diagnostics, "doc", "0" * 64, region,
                {"line_segments": {"horizontal": [[0, 1, 2, 3]]}},
            )
            payload = __import__("json").loads((run / ref["path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], VISION_DIAGNOSTIC_SCHEMA_VERSION)
            self.assertIn("page-relative", payload["coordinate_formats"]["region_coordinates"])
            self.assertIn("region-relative", payload["coordinate_formats"]["line_segments"])
            self.assertEqual(len(ref["sha256"]), 64)

    def test_overlapping_visual_objects_merge(self) -> None:
        merged = merge_visual_objects(
            [(100, 100, 300, 300), (200, 150, 400, 350), (700, 100, 800, 200)], 1000, 1000,
        )
        self.assertEqual(sorted(len(item["image_indices"]) for item in merged), [1, 2])

    def test_footer_band_does_not_merge_with_chart(self) -> None:
        merged = merge_visual_objects(
            [(200, 300, 700, 850), (0, 855, 1000, 1000)], 1000, 1000,
        )
        self.assertEqual(len(merged), 2)

    def test_adjacent_visual_objects_merge(self) -> None:
        merged = merge_visual_objects(
            [(100, 100, 300, 300), (305, 120, 500, 310)], 1000, 1000,
        )
        self.assertEqual(len(merged), 1)

    def test_visual_title_prefers_chart_heading(self) -> None:
        lines = [
            {"text": "Office", "coordinates": [100, 200, 40, 20]},
            {"text": "Portfolio Allocation", "coordinates": [100, 100, 180, 30]},
        ]
        self.assertEqual(_visual_title("chart", lines), "Portfolio Allocation")

    def test_visual_title_allows_numeric_portfolio_title(self) -> None:
        lines = [
            {"text": "$146 Million Portfolio", "coordinates": [100, 100, 180, 30]},
            {"text": "Founders", "coordinates": [400, 200, 80, 20]},
        ]
        self.assertEqual(_visual_title("chart", lines), "$146 Million Portfolio")

    def test_numeric_percentage_is_normalized(self) -> None:
        self.assertEqual(_numeric_value("15.9%"), (15.9, "percent", 0.159))

    def test_pie_type_from_multiple_raster_marks(self) -> None:
        region = Region(
            "p007-r001", 7, "visual", [100, 100, 700, 700], 1, "merge", 0.8,
            metadata={"image_indices": [1, 2, 3, 4]},
        )
        lines = [{"text": value} for value in ("6.1%", "15.9%", "37.5%")]
        self.assertEqual(_infer_chart_type(region, lines, {}, None), "pie")

    def test_proximity_binding_is_unique_and_evidence_linked(self) -> None:
        lines = [
            {"evidence_id": "label-a", "text": "Office", "coordinates": [100, 100, 60, 20]},
            {"evidence_id": "value-a", "text": "6.1%", "coordinates": [105, 125, 50, 20]},
            {"evidence_id": "label-b", "text": "Land", "coordinates": [400, 100, 60, 20]},
            {"evidence_id": "value-b", "text": "7.7%", "coordinates": [405, 125, 50, 20]},
        ]
        bindings = _proximity_bindings(lines, None, [[80, 80, 120, 120], [380, 80, 120, 120]])
        self.assertEqual({item["label"] for item in bindings}, {"Office", "Land"})
        self.assertEqual(len({item["label_evidence_id"] for item in bindings}), 2)
        self.assertTrue(all(item["visual_mark_coordinates"] for item in bindings))

    def test_page7_ground_truth_regression(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures/coulton-page-007-pie.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        marks = [[150, 250, 700, 600]] * len(fixture["expected_observations"])
        bindings = _proximity_bindings(fixture["ocr_lines"], fixture["chart_title"], marks)
        actual = {item["label"]: item["value"] for item in bindings}
        self.assertEqual(actual, fixture["expected_observations"])
        self.assertAlmostEqual(
            sum(item["numeric_value"] for item in bindings if item["unit"] == "percent"),
            fixture["expected_percentage_total"], places=5,
        )
        self.assertEqual(len({item["value_evidence_id"] for item in bindings}), 10)

    def test_repeated_footer_is_classified_as_decoration(self) -> None:
        inspections = []
        for page in range(1, 4):
            region = Region(f"p{page:03d}-r001", page, "visual", [0, 870, 1000, 130], 1, "image", 0.58)
            inspections.append(PageInspection(
                page, 720, 540, 0, True, 0.1, 0.9, 0.1, 0, 0, 1, 0.58, [region], [],
            ))
        mark_repeated_decorations(inspections)
        self.assertTrue(all(page.regions[0].kind == "decoration" for page in inspections))
        self.assertTrue(all(page.regions[0].confidence == 0.98 for page in inspections))

    def test_vector_bars_create_one_chart_region(self) -> None:
        rectangles = [
            {"x0": x, "top": top, "x1": x + 20, "bottom": 400, "fill": True}
            for x, top in [(100, 300), (160, 250), (220, 280), (280, 210)]
        ]
        rectangles.append({"x0": 0, "top": 0, "x1": 720, "bottom": 540, "fill": False})
        visual = detect_vector_bar_chart(rectangles, 720, 540)
        self.assertIsNotNone(visual)
        self.assertEqual(visual["chart_type_hint"], "bar")
        self.assertEqual(visual["expected_mark_count"], 4)

    def test_small_square_rasters_create_scatterplot_region(self) -> None:
        images = [(100 + index * 12, 200 + index % 3 * 10, 110 + index * 12, 210 + index % 3 * 10) for index in range(10)]
        visual = detect_small_raster_chart(images, 720, 540)
        self.assertIsNotNone(visual)
        self.assertEqual(visual["chart_type_hint"], "scatterplot")
        self.assertEqual(visual["expected_mark_count"], 10)

    def test_aligned_financial_table_infers_columns_without_document_labels(self) -> None:
        def word(text: str, x0: float, top: float, width: float = 30) -> dict[str, object]:
            return {"text": text, "x0": x0, "x1": x0 + width, "top": top, "bottom": top + 10}

        words = [
            word("Asset", 250, 90), word("Schedule", 310, 90),
            word("Asset", 30, 140), word("Cost", 240, 140),
            word("Share", 440, 140), word("Debt", 625, 140),
        ]
        for index in range(6):
            top = 190 + index * 20
            words.extend([
                word(f"Holding-{index + 1}", 30, top, 80),
                word(f"{index + 1}", 260, top, 5),
                word("00,000", 264, top, 46),
                word(f"{index + 1}.0%", 445, top, 35),
                word(f"{index + 2}50,000", 630, top, 50),
            ])
        rectangles = [
            {"x0": 10, "top": 124, "x1": 710, "bottom": 175, "fill": True},
            {"x0": 10, "top": 390, "x1": 710, "bottom": 408, "fill": True},
        ]
        table = detect_aligned_financial_table(words, rectangles, 720, 540)
        self.assertIsNotNone(table)
        self.assertEqual(len(table["rows"][0]), 4)
        self.assertEqual(table["rows"][1][1], "100,000")
        self.assertEqual(table["title"], "Asset Schedule")
        self.assertEqual(table["classification_method"], "native-positioned-aligned-financial-table")

    def test_aligned_financial_table_rejects_layout_without_numeric_anchors(self) -> None:
        words = [
            {"text": "Narrative", "x0": 30, "x1": 90, "top": 140, "bottom": 150},
            {"text": "copy", "x0": 30, "x1": 60, "top": 200, "bottom": 210},
        ]
        rectangles = [{"x0": 10, "top": 124, "x1": 710, "bottom": 175, "fill": True}]
        self.assertIsNone(detect_aligned_financial_table(words, rectangles, 720, 540))

    def test_adjacent_numeric_fragments_are_joined_before_column_assignment(self) -> None:
        fragments = [
            {"text": "3", "x0": 517.5, "x1": 523.2, "top": 362.9, "bottom": 372.9},
            {"text": "1,031,365", "x0": 522.9, "x1": 567.1, "top": 362.9, "bottom": 372.9},
        ]
        merged = _merge_numeric_fragments(fragments, 720)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"], "31,031,365")

    def test_vector_bar_bindings_use_category_value_and_mark(self) -> None:
        lines = [
            {"evidence_id": "v1", "text": "11.3%", "coordinates": [105, 300, 20, 50]},
            {"evidence_id": "v2", "text": "12.9%", "coordinates": [205, 280, 20, 50]},
            {"evidence_id": "v3", "text": "16.7%", "coordinates": [305, 230, 20, 50]},
            {"evidence_id": "c1", "text": "2014", "coordinates": [100, 510, 40, 20]},
            {"evidence_id": "c2", "text": "2015", "coordinates": [200, 510, 40, 20]},
            {"evidence_id": "c3", "text": "2016", "coordinates": [300, 510, 40, 20]},
        ]
        marks = [[100, 200, 30, 300], [200, 180, 30, 320], [300, 130, 30, 370]]
        bindings = _bar_bindings(lines, "Annual Returns", marks)
        self.assertEqual([(item["label"], item["value"]) for item in bindings], [
            ("2014", "11.3%"), ("2015", "12.9%"), ("2016", "16.7%"),
        ])
        self.assertEqual(len({tuple(item["visual_mark_coordinates"]) for item in bindings}), 3)
        self.assertTrue(all(item["grounding_method"].startswith("x-aligned") for item in bindings))

    def test_generic_subtotals_reconcile(self) -> None:
        rows = [
            ["Holdings", "Cost", "Share", "Exposure"],
            ["Total Region North", "$60,000", "40.0%", "$45,000"],
            ["Total Region South", "$90,000", "60.0%", "$55,000"],
            ["Grand Total Holdings", "$150,000", "100.0%", "$100,000"],
        ]
        result = _table_total_reconciliation(rows)
        self.assertIsNotNone(result)
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["checks"]), 3)
        self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_generic_subtotal_mismatch_is_detected(self) -> None:
        rows = [
            ["Holdings", "Cost"],
            ["Total Segment One", "40"],
            ["Total Segment Two", "50"],
            ["Total Holdings", "100"],
        ]
        result = _table_total_reconciliation(rows)
        self.assertIsNotNone(result)
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()

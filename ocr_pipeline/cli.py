from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PDF ingestion through a page-organized Unified Source Block collection"
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, help="New immutable run directory")
    parser.add_argument("--pages", help="Page selection such as 1-3,8")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--native-threshold", type=float, default=0.78)
    parser.add_argument("--route-threshold", type=float, default=0.72)
    parser.add_argument("--skip-ocr", action="store_true", help="Native-PDF smoke tests only")
    parser.add_argument("--rapidocr-python", type=Path)
    parser.add_argument("--rapidocr-models", type=Path)
    parser.add_argument("--pdftoppm", type=Path)
    parser.add_argument("--qwen-endpoint", help="OpenAI-compatible chat-completions endpoint")
    parser.add_argument("--qwen-model", default="Qwen3-VL")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = args.output
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = Path(__file__).resolve().parents[1] / "runs" / f"{args.pdf.stem}-{stamp}"
    result = run_pipeline(
        args.pdf, output, pages_spec=args.pages, dpi=args.dpi,
        native_threshold=args.native_threshold, route_threshold=args.route_threshold,
        skip_ocr=args.skip_ocr, rapidocr_python=args.rapidocr_python,
        rapidocr_models=args.rapidocr_models, pdftoppm=args.pdftoppm,
        qwen_endpoint=args.qwen_endpoint, qwen_model=args.qwen_model,
    )
    print(f"Unified Source Block collection: {result / 'source-blocks' / 'document-manifest.json'}")
    return 0


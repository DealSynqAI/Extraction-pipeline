"""Reproducible image-only Qwen3-VL ablation over an existing full-run page set.

The model receives one rendered page image and a generic schema instruction. It
receives no OCR transcript, PDF object coordinates, OpenCV features, previous
page extraction, or reference answers. JSON parsing is the only postprocessing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_MODEL = "dealsynq-qwen3-vl:4b-instruct-16k"
DEFAULT_ENDPOINT = "http://localhost:11434/v1/chat/completions"

ITEM = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "value": {"type": "string"},
        "unit": {"type": "string"},
        "parent": {"type": "string"},
    },
    "required": ["label", "value", "unit", "parent"],
    "additionalProperties": False,
}
CELL = {
    "type": "object",
    "properties": {
        "column": {"type": "string"},
        "value": {"type": "string"},
    },
    "required": ["column", "value"],
    "additionalProperties": False,
}
ROW = {
    "type": "object",
    "properties": {
        "section": {"type": "string"},
        "label": {"type": "string"},
        "cells": {"type": "array", "items": CELL, "maxItems": 16},
    },
    "required": ["section", "label", "cells"],
    "additionalProperties": False,
}
BLOCK = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["heading", "text", "table", "chart", "map", "photograph",
                     "comparison_panel", "contact", "brand_mark", "decoration"],
        },
        "title": {"type": "string"},
        "text": {"type": "string"},
        "items": {"type": "array", "items": ITEM, "maxItems": 130},
        "rows": {"type": "array", "items": ROW, "maxItems": 50},
    },
    "required": ["type", "title", "text", "items", "rows"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "page_number": {"type": "integer", "minimum": 1},
        "page_title": {"type": "string"},
        "blocks": {"type": "array", "items": BLOCK, "maxItems": 80},
    },
    "required": ["page_number", "page_title", "blocks"],
    "additionalProperties": False,
}

PROMPT_BASELINE = """Extract this single document page using the IMAGE ALONE. Output JSON matching
the supplied schema. Do not use outside knowledge. Do not invent unreadable
values. Include all meaningful headings and text, and keep separate visual
objects separate. For a table, make one table block and put each visible row in
rows with correctly headed cells. For a chart, put each category/value/unit
association in items, with its own chart block if there are multiple charts or
an inset. For a map, associate each displayed value with the geographic region
that visibly owns it. If region names are unprinted, use recognizable outlines
to infer a name only when reasonably confident; never infer a value from color
alone. For comparison cards, preserve each lane's
heading and claim as separate parent-labelled items. For dense plots, do not
turn axis ticks into observations; state when individual plotted values cannot
be read. For body/legal text, transcribe it in the correct paragraph and column
order. In items, parent names the containing chart, subsection, or category.
Use empty strings/arrays where a field does not apply. Exact printed numbers,
negations, and units matter. No explanation outside JSON."""

PROMPT_COMPACT = """Extract this single document page using the IMAGE ALONE. Output JSON matching
the supplied schema. Do not use outside knowledge. Do not invent unreadable
values. Include all meaningful headings and text, and keep separate visual
objects separate. For a table, make one table block and put each visible row in
rows with correctly headed cells. For a chart, put each category/value/unit
association in items, with its own chart block if there are multiple charts or
an inset. A dashed connector can indicate that a smaller chart subdivides a
larger category; keep those charts and their denominators distinct. For a map,
associate each displayed value with the geographic region
that visibly owns it. If region names are unprinted, use recognizable outlines
to infer a name only when reasonably confident; never infer a value from color
alone. Each visibly printed numeric map label may own at most one region. Do
not repeat a state or copy one number to many regions; omit regions with no
printed numeric value. For comparison cards, preserve each lane's
heading and claim as separate parent-labelled items. For dense plots, do not
turn axis ticks into observations; state when individual plotted values cannot
be read. For body/legal text, transcribe it in the correct paragraph and column
order. In items, parent names the containing chart, subsection, or category.
For ordinary text/heading blocks, put the prose only in text, not copied into
items. For photographs and brand marks, use at most one short description in
text and leave items empty. Never repeat a sentence. Use empty strings/arrays
where a field does not apply. Exact printed numbers, negations, and units matter.
No explanation outside JSON."""


def run_page(
    image: Path, page_number: int, endpoint: str, model: str, timeout: int,
    raw_output: Path, prompt: str,
) -> tuple[dict, str, dict]:
    data = image.read_bytes()
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 8192,
        "enable_thinking": False,
        "think": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "vision_only_page", "strict": True, "schema": SCHEMA},
        },
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Document page metadata: page {page_number}.\n\n{prompt}"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")}},
            ],
        }],
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        answer = json.loads(response.read().decode("utf-8"))
    seconds = time.perf_counter() - start
    raw = answer["choices"][0]["message"]["content"]
    if isinstance(raw, list):
        raw = "".join(item.get("text", "") for item in raw if isinstance(item, dict))
    raw = str(raw).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    raw_output.write_text(raw, encoding="utf-8")
    parsed = json.loads(raw)
    receipt = {
        "page": page_number,
        "status": "complete",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "endpoint": endpoint,
        "page_image": str(image),
        "page_image_sha256": hashlib.sha256(data).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "seconds": round(seconds, 3),
        "input_tokens": (answer.get("usage") or {}).get("prompt_tokens"),
        "output_tokens": (answer.get("usage") or {}).get("completion_tokens"),
        "finish_reason": answer["choices"][0].get("finish_reason"),
        "response_schema": "vision-only-page-lite/1.0",
        "input_policy": "page image + generic prompt only; no OCR/PDF/OpenCV/reference",
    }
    return parsed, raw, receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True, help="Existing full-run page-images folder")
    parser.add_argument("--output", type=Path, required=True, help="New or existing result folder")
    parser.add_argument("--pages", default="1-16")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt-profile", choices=("baseline", "compact"), default="baseline")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    prompt = PROMPT_BASELINE if args.prompt_profile == "baseline" else PROMPT_COMPACT
    if "-" in args.pages:
        first, last = (int(part) for part in args.pages.split("-", 1))
        pages = range(first, last + 1)
    else:
        pages = [int(part) for part in args.pages.split(",")]
    args.output.mkdir(parents=True, exist_ok=True)
    for page in pages:
        stem = f"page-{page:03d}"
        image = args.images / f"{stem}.png"
        target = args.output / f"{stem}.json"
        if target.exists():
            print(f"{stem}: already recorded; skip", flush=True)
            continue
        print(f"{stem}: calling {args.model}", flush=True)
        start = time.perf_counter()
        try:
            parsed, raw, receipt = run_page(
                image, page, args.endpoint, args.model, args.timeout,
                args.output / f"{stem}.raw.txt", prompt,
            )
            if parsed.get("page_number") != page:
                receipt["page_number_mismatch"] = parsed.get("page_number")
            target.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            (args.output / f"{stem}.receipt.json").write_text(
                json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"{stem}: complete in {receipt['seconds']:.1f}s; {len(parsed.get('blocks', []))} blocks", flush=True)
        except Exception as exc:
            receipt = {
                "page": page,
                "status": "failed",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "model": args.model,
                "prompt_profile": args.prompt_profile,
                "seconds": round(time.perf_counter() - start, 3),
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            }
            (args.output / f"{stem}.receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            print(f"{stem}: FAILED {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    main()

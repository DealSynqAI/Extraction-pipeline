from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from typing import Any


def normalize_vision_payload(payload: Any) -> dict[str, Any]:
    """Normalize a model response to the small routing contract consumed by the pipeline."""
    if not isinstance(payload, dict):
        raise ValueError("vision model response must be a JSON object")
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    bindings = payload.get("bindings", [])
    if not isinstance(bindings, list):
        bindings = []
    bindings = [binding for binding in bindings if isinstance(binding, dict)]
    chart_type = payload.get("chart_type")
    return {
        "type": str(payload.get("type") or "").strip().lower(),
        "confidence": confidence,
        "chart_type": None if chart_type is None else str(chart_type).strip().lower(),
        "bindings": bindings,
    }


def discover_rapidocr_python() -> Path | None:
    candidates = [Path(sys.executable)]
    configured = os.environ.get("DEALSYNQ_RAPIDOCR_PYTHON")
    if configured:
        candidates.insert(0, Path(configured))
    for candidate in candidates:
        if not candidate.exists():
            continue
        result = subprocess.run(
            [str(candidate), "-c", "import rapidocr,cv2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return candidate.resolve()
    return None


def discover_model_root() -> Path | None:
    configured = os.environ.get("DEALSYNQ_RAPIDOCR_MODELS")
    if not configured:
        return None
    path = Path(configured)
    return path.resolve() if path.is_dir() else None


def run_rapidocr_worker(
    jobs: list[dict[str, Any]], work_dir: Path, python_executable: Path | None = None,
    model_root: Path | None = None,
) -> dict[str, Any]:
    python_executable = python_executable or discover_rapidocr_python()
    if python_executable is None:
        raise RuntimeError(
            "RapidOCR is unavailable. Install the 'ocr' extra or set DEALSYNQ_RAPIDOCR_PYTHON."
        )
    request_path = work_dir / "rapidocr-jobs.json"
    result_path = work_dir / "rapidocr-results.json"
    request_path.write_text(json.dumps({"jobs": jobs}, indent=2) + "\n", encoding="utf-8")
    command = [
        str(python_executable), str(Path(__file__).with_name("rapidocr_worker.py")),
        "--jobs", str(request_path), "--output", str(result_path),
    ]
    model_root = model_root or discover_model_root()
    if model_root:
        command.extend(["--model-root", str(model_root)])
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    (work_dir / "rapidocr.log").write_text(
        completed.stdout + ("\nSTDERR\n" + completed.stderr if completed.stderr else ""), encoding="utf-8"
    )
    if completed.returncode:
        raise RuntimeError(f"RapidOCR worker failed with exit code {completed.returncode}; see {work_dir / 'rapidocr.log'}")
    return json.loads(result_path.read_text(encoding="utf-8"))


class QwenVisionClient:
    """Small OpenAI-compatible adapter used only for classification and visual grounding."""

    def __init__(self, endpoint: str, model: str, api_key_env: str = "QWEN_API_KEY", timeout: int = 120):
        self.endpoint = endpoint
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout

    def analyze(self, image_path: Path, prompt: str) -> dict[str, Any]:
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }],
        }
        headers = {"Content-Type": "application/json"}
        token = os.environ.get(self.api_key_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        content = str(content).strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            if content.lstrip().startswith("json"):
                content = content.lstrip()[4:].lstrip()
        return normalize_vision_payload(json.loads(content))

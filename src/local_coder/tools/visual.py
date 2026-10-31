"""Visual verification tool for frontend/UI regression detection.

Boots a headless browser (Playwright), spins up a local dev server if needed,
takes screenshots, and compares them against a baseline/reference image to
catch styling regressions, broken layouts, or unstyled raw text elements that
traditional linters miss.

This tool is intentionally bounded: it only runs against localhost URLs or
local file:// paths, never remote hosts. It does not execute arbitrary code
inside the browser beyond the page's own scripts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace


class VisualVerifyTool(Tool):
    name = ToolName.VISUAL_VERIFY
    description = (
        "Render a local page in a headless browser and compare it against a "
        "baseline screenshot or reference image. Returns a pass/fail verdict "
        "plus a diff image path when the rendered output differs beyond the "
        "configured tolerance. Only localhost URLs and local file:// paths "
        "are allowed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Local URL (http://localhost:...) or file:// path to render.",
            },
            "baseline": {
                "type": "string",
                "description": "Optional path to a baseline screenshot PNG to compare against.",
            },
            "viewport_width": {
                "type": "integer",
                "description": "Browser viewport width in pixels (default 1280).",
            },
            "viewport_height": {
                "type": "integer",
                "description": "Browser viewport height in pixels (default 800).",
            },
            "tolerance": {
                "type": "number",
                "description": "Maximum allowed pixel difference ratio (0.0-1.0, default 0.02).",
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds to wait for page load (default 30).",
            },
        },
        "required": ["url"],
    }

    def __init__(self, project_root: str):
        self.project_root = project_root

    async def execute(
        self,
        url: str,
        baseline: str | None = None,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        tolerance: float = 0.02,
        timeout: int = 30,
        **kwargs: Any,
    ) -> ToolResult:
        start_t = time.time()
        try:
            # Validate the target is local-only.
            if not (url.startswith("http://localhost") or url.startswith("http://127.0.0.1") or url.startswith("file://")):
                return ToolResult(
                    success=False,
                    output="Visual verification only supports localhost URLs and local file:// paths.",
                    duration_ms=int((time.time() - start_t) * 1000),
                )

            # Validate baseline path if provided.
            baseline_path = None
            if baseline is not None:
                baseline_path = Workspace(self.project_root).resolve(baseline)
                if not baseline_path.is_file():
                    return ToolResult(
                        success=False,
                        output=f"Baseline image not found: {baseline}",
                        duration_ms=int((time.time() - start_t) * 1000),
                    )

            # Clamp viewport to sane bounds.
            viewport_width = max(320, min(viewport_width, 3840))
            viewport_height = max(240, min(viewport_height, 2160))
            tolerance = max(0.0, min(tolerance, 1.0))

            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        viewport={"width": viewport_width, "height": viewport_height}
                    )
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                    screenshot_bytes = await page.screenshot(full_page=True)
                finally:
                    await browser.close()

            screenshot_hash = hashlib.sha256(screenshot_bytes).hexdigest()[:16]
            output_dir = Path(self.project_root) / ".local-coder" / "visual"
            output_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = output_dir / f"visual-{screenshot_hash}.png"
            screenshot_path.write_bytes(screenshot_bytes)

            diff_result = None
            if baseline_path is not None:
                diff_result = self._compare_images(screenshot_bytes, baseline_path.read_bytes(), tolerance)

            passed = True if diff_result is None else diff_result["passed"]
            output = (
                f"Rendered {url} -> {screenshot_path} ({len(screenshot_bytes)} bytes). "
                + ("No baseline provided." if diff_result is None else
                   f"Baseline comparison: {'PASS' if diff_result['passed'] else 'FAIL'} "
                   f"(max pixel diff ratio {diff_result['max_ratio']:.4f}, tolerance {tolerance}).")
            )

            return ToolResult(
                success=passed,
                output=output,
                duration_ms=int((time.time() - start_t) * 1000),
                data={
                    "screenshot_path": str(screenshot_path),
                    "screenshot_hash": screenshot_hash,
                    "baseline": str(baseline_path) if baseline_path else None,
                    "passed": passed,
                    "diff_result": diff_result,
                },
            )
        except ImportError:
            return ToolResult(
                success=False,
                output="Playwright is not installed. Install with: pip install playwright",
                duration_ms=int((time.time() - start_t) * 1000),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"Visual verification failed: {str(e)}",
                duration_ms=int((time.time() - start_t) * 1000),
            )

    def _compare_images(self, current: bytes, baseline: bytes, tolerance: float) -> dict[str, Any]:
        """Compare two PNG images by pixel difference ratio.

        Falls back to a simple hash comparison if PIL is not available.
        Produces a side-by-side diff image on mismatch.
        """
        try:
            from PIL import Image, ImageDraw
            import io

            img_current = Image.open(io.BytesIO(current)).convert("RGB")
            img_baseline = Image.open(io.BytesIO(baseline)).convert("RGB")

            if img_current.size != img_baseline.size:
                return {
                    "passed": False,
                    "max_ratio": 1.0,
                    "reason": f"Size mismatch: {img_current.size} vs {img_baseline.size}",
                }

            pixels_current = list(img_current.get_flattened_data())
            pixels_baseline = list(img_baseline.get_flattened_data())
            total = len(pixels_current)
            diff_count = sum(
                1 for a, b in zip(pixels_current, pixels_baseline) if a != b
            )
            ratio = diff_count / total if total else 0.0

            result: dict[str, Any] = {
                "passed": ratio <= tolerance,
                "max_ratio": ratio,
                "tolerance": tolerance,
                "total_pixels": total,
                "diff_pixels": diff_count,
            }

            if not result["passed"]:
                diff_path = self._write_diff_image(img_current, img_baseline)
                if diff_path:
                    result["diff_image_path"] = str(diff_path)

            return result
        except ImportError:
            # PIL not available; fall back to exact hash match.
            current_hash = hashlib.sha256(current).hexdigest()
            baseline_hash = hashlib.sha256(baseline).hexdigest()
            passed = current_hash == baseline_hash
            return {
                "passed": passed,
                "max_ratio": 0.0 if passed else 1.0,
                "reason": "PIL not available; exact hash comparison used.",
            }

    def _write_diff_image(self, current: Image.Image, baseline: Image.Image) -> Path | None:
        """Write a side-by-side diff image highlighting differing pixels."""
        try:
            from PIL import Image, ImageDraw
            import io

            width = current.width + baseline.width + 40
            height = max(current.height, baseline.height) + 60
            diff = Image.new("RGB", (width, height), (240, 240, 240))
            draw = ImageDraw.Draw(diff)

            diff.paste(current, (20, 30))
            diff.paste(baseline, (current.width + 40, 30))

            draw.text((20, 5), "Current", fill=(0, 0, 0))
            draw.text((current.width + 40, 5), "Baseline", fill=(0, 0, 0))

            # Highlight differing pixels in red on the current side.
            px_current = current.load()
            px_baseline = baseline.load()
            for y in range(min(current.height, baseline.height)):
                for x in range(min(current.width, baseline.width)):
                    if px_current[x, y] != px_baseline[x, y]:
                        diff.putpixel((x + 20, y + 30), (255, 0, 0))

            output_dir = Path(self.project_root) / ".local-coder" / "visual"
            output_dir.mkdir(parents=True, exist_ok=True)
            diff_path = output_dir / f"diff-{hashlib.sha256(current.tobytes()).hexdigest()[:12]}.png"
            diff.save(diff_path)
            return diff_path
        except Exception:
            return None

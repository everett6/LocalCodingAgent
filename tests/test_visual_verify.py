"""Tests for the visual verification tool."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image
import io

from local_coder.tools.visual import VisualVerifyTool
from local_coder.types import ToolResult


def run(coro):
    return asyncio.run(coro)


def _make_png(color=(255, 0, 0), size=(10, 10)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_visual_verify_rejects_remote_url(tmp_path):
    tool = VisualVerifyTool(str(tmp_path))
    result = run(tool.execute(url="https://evil.example.com/page"))
    assert not result.success
    assert "localhost" in result.output or "file://" in result.output


def test_visual_verify_rejects_remote_baseline(tmp_path):
    tool = VisualVerifyTool(str(tmp_path))
    result = run(tool.execute(url="http://localhost:8080", baseline="/etc/passwd"))
    assert not result.success
    assert "escapes workspace" in result.output or "Baseline image not found" in result.output


def test_visual_verify_missing_playwright_returns_helpful_error(tmp_path):
    tool = VisualVerifyTool(str(tmp_path))
    with patch("playwright.async_api.async_playwright", side_effect=ImportError("No module named 'playwright'")):
        result = run(tool.execute(url="http://localhost:8080"))
    assert not result.success
    assert "Playwright is not installed" in result.output


def test_visual_verify_successful_render(tmp_path):
    tool = VisualVerifyTool(str(tmp_path))
    fake_page = AsyncMock()
    fake_page.screenshot.return_value = _make_png((255, 0, 0))
    fake_context = AsyncMock()
    fake_context.new_page.return_value = fake_page
    fake_browser = AsyncMock()
    fake_browser.new_context.return_value = fake_context

    with patch("playwright.async_api.async_playwright") as mock_playwright:
        mock_playwright.return_value.__aenter__.return_value.chromium.launch.return_value = fake_browser
        result = run(tool.execute(url="http://localhost:8080"))

    assert result.success
    assert "Rendered" in result.output
    assert result.data["passed"] is True
    assert result.data["baseline"] is None


def test_visual_verify_baseline_comparison_pass(tmp_path):
    tool = VisualVerifyTool(str(tmp_path))
    fake_page = AsyncMock()
    fake_page.screenshot.return_value = _make_png((255, 0, 0))
    fake_context = AsyncMock()
    fake_context.new_page.return_value = fake_page
    fake_browser = AsyncMock()
    fake_browser.new_context.return_value = fake_context

    baseline_path = tmp_path / "baseline.png"
    baseline_path.write_bytes(_make_png((255, 0, 0)))

    with patch("playwright.async_api.async_playwright") as mock_playwright:
        mock_playwright.return_value.__aenter__.return_value.chromium.launch.return_value = fake_browser
        result = run(tool.execute(url="http://localhost:8080", baseline=str(baseline_path)))

    assert result.success
    assert result.data["passed"] is True
    assert result.data["diff_result"]["passed"] is True


def test_visual_verify_baseline_comparison_fail(tmp_path):
    tool = VisualVerifyTool(str(tmp_path))
    fake_page = AsyncMock()
    fake_page.screenshot.return_value = _make_png((255, 0, 0))
    fake_context = AsyncMock()
    fake_context.new_page.return_value = fake_page
    fake_browser = AsyncMock()
    fake_browser.new_context.return_value = fake_context

    baseline_path = tmp_path / "baseline.png"
    baseline_path.write_bytes(_make_png((0, 255, 0)))

    with patch("playwright.async_api.async_playwright") as mock_playwright:
        mock_playwright.return_value.__aenter__.return_value.chromium.launch.return_value = fake_browser
        result = run(tool.execute(url="http://localhost:8080", baseline=str(baseline_path)))

    assert not result.success
    assert result.data["passed"] is False
    assert result.data["diff_result"]["passed"] is False


def test_visual_verify_file_url(tmp_path):
    tool = VisualVerifyTool(str(tmp_path))
    fake_page = AsyncMock()
    fake_page.screenshot.return_value = b"fake-png-bytes"
    fake_context = AsyncMock()
    fake_context.new_page.return_value = fake_page
    fake_browser = AsyncMock()
    fake_browser.new_context.return_value = fake_context

    html_file = tmp_path / "index.html"
    html_file.write_text("<html><body>Hello</body></html>")

    with patch("playwright.async_api.async_playwright") as mock_playwright:
        mock_playwright.return_value.__aenter__.return_value.chromium.launch.return_value = fake_browser
        result = run(tool.execute(url=f"file://{html_file}"))

    assert result.success
    assert "Rendered" in result.output
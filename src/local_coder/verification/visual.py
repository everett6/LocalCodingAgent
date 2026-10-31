"""Opt-in evidence and explicit DOM assertions for an existing local UI.

Playwright and its Chromium binary are optional operator-installed dependencies.
Browser request filtering is not an OS network sandbox or a vision assessment.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import math
import os
from pathlib import Path
import stat
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from local_coder.file_lock import exclusive_lock

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACTS = 50
MAX_PROJECT_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_RESOURCE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class VisualUnavailable(RuntimeError):
    """Optional local browser support cannot enforce the requested check."""


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{host}:{port}"


def validate_visual_request(
    url: str, *, assertions: list[dict] | None = None,
    viewport: dict | None = None, timeout_seconds: int = 15,
) -> dict:
    """Validate before importing a browser or starting a process."""
    if (not isinstance(url, str) or not 1 <= len(url) <= 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or any(char in url for char in ("\\", "?", "#"))):
        raise ValueError("Choose a literal loopback HTTP URL without credentials, query or fragment")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
        origin = _origin(url)
    except ValueError:
        raise ValueError("Choose a literal loopback HTTP URL without credentials, query or fragment") from None
    assertions = [] if assertions is None else assertions
    if not isinstance(assertions, list) or len(assertions) > 20:
        raise ValueError("Provide at most 20 explicit UI assertions")
    clean = []
    for row in assertions:
        if (not isinstance(row, dict) or set(row) - {"selector", "visible", "text"}
                or not isinstance(row.get("selector"), str) or not 1 <= len(row["selector"]) <= 256
                or type(row.get("visible", True)) is not bool
                or ("text" in row and (not isinstance(row["text"], str) or not 1 <= len(row["text"]) <= 512))):
            raise ValueError("Each UI assertion needs a bounded selector and optional visibility/text expectation")
        clean.append({**row, "visible": row.get("visible", True)})
    viewport = {"width": 1280, "height": 800} if viewport is None else viewport
    if (not isinstance(viewport, dict) or set(viewport) != {"width", "height"}
            or type(viewport["width"]) is not int or not 320 <= viewport["width"] <= 1920
            or type(viewport["height"]) is not int or not 240 <= viewport["height"] <= 1080):
        raise ValueError("Viewport must be 320–1920 pixels wide and 240–1080 pixels high")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("UI verification timeout must be 1–60 seconds")
    return {"url": url, "origin": origin, "assertions": clean,
            "viewport": dict(viewport), "timeout_seconds": timeout_seconds}


def _load_playwright_factory():
    from playwright.async_api import async_playwright
    return async_playwright


@asynccontextmanager
async def _credential_free_stream(client, method, url, headers):
    outbound = client.build_request(method, url, headers=headers)
    # A shared HTTP client may have received Set-Cookie from an earlier local
    # response. Remove its merged cookie header from every individual request.
    outbound.headers.pop("cookie", None)
    response = await client.send(outbound, stream=True, follow_redirects=False)
    try:
        yield response
    finally:
        await response.aclose()


def _store_artifact(project_root: Path, data: bytes) -> dict:
    """Write through private directory descriptors; no caller-selected filename."""
    if (not isinstance(data, bytes) or not data.startswith(PNG_HEADER)
            or len(data) > MAX_ARTIFACT_BYTES):
        raise ValueError("Screenshot exceeded the PNG artifact limit")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise VisualUnavailable("Safe UI artifact storage is unavailable on this platform")
    descriptors = []
    parts = [".local-coder", "artifacts", "ui"]
    filename = uuid4().hex + ".png"
    try:
        descriptors.append(os.open(project_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptors[-1])
            except FileExistsError:
                pass
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1])
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ValueError("UI artifact directory must belong to the current user")
            os.fchmod(descriptor, 0o700)
        lock_fd = os.open("artifact.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                          0o600, dir_fd=descriptors[-1])
        with os.fdopen(lock_fd, "r+") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Unsafe UI artifact lock")
            with exclusive_lock(lock, blocking=False):
                total, count = 0, 0
                with os.scandir(descriptors[-1]) as entries:
                    for entry in entries:
                        if entry.name == "artifact.lock":
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError("Unexpected file in UI artifact storage")
                        count += 1
                        total += info.st_size
                        if count >= MAX_ARTIFACTS or total + len(data) > MAX_PROJECT_ARTIFACT_BYTES:
                            raise ValueError("UI artifact retention limit reached; review existing screenshots before continuing")
                descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=descriptors[-1])
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                except BaseException:
                    os.unlink(filename, dir_fd=descriptors[-1])
                    raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return {"path": "/".join([*parts, filename]), "mime_type": "image/png",
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class VisualVerifier:
    """Capture a viewport and observed assertions; never start a server/model."""

    def __init__(self, project_root: str | Path, *, playwright_factory=None, http_transport=None):
        self.project_root = Path(project_root).resolve()
        self.playwright_factory = playwright_factory
        self.http_transport = http_transport

    async def verify(self, url: str, *, assertions=None, viewport=None, timeout_seconds=15) -> dict:
        request = validate_visual_request(url, assertions=assertions, viewport=viewport,
                                          timeout_seconds=timeout_seconds)
        started = time.monotonic()
        report: dict[str, Any] = {
            "status": "unavailable", "origin": request["origin"], "viewport": request["viewport"],
            "assertions": [], "console_errors": 0, "page_errors": 0,
            "network_requests_blocked": 0, "websockets_blocked": 0,
            "resource_requests": 0, "resource_bytes": 0,
            "horizontal_overflow": None, "http_status": None, "artifact": None,
            "navigation_started": False,
            "vision_assessment": "not_run",
            "limitation": "Observed DOM assertions and viewport evidence only; no visual-model assessment or OS network isolation.",
        }
        playwright = browser = context = client = None
        navigation_started = False

        def count(key):
            report[key] = min(1000, report[key] + 1)

        async def restrict(route):
            target = route.request.url
            try:
                parsed = urlsplit(target)
                allowed = (parsed.scheme in {"http", "https"} and parsed.username is None and parsed.password is None
                           and parsed.hostname in {"127.0.0.1", "::1"}
                           and _origin(target) == request["origin"]
                           and not any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in target)
                           and route.request.method in {"GET", "HEAD", "OPTIONS"})
            except (TypeError, ValueError):
                allowed = False
            report["resource_requests"] = min(201, report["resource_requests"] + 1)
            if not allowed or report["resource_requests"] > 200 or report["resource_bytes"] >= MAX_RESPONSE_BYTES:
                count("network_requests_blocked")
                await route.abort()
                return
            headers = {key: value for key, value in getattr(route.request, "headers", {}).items()
                       if key.lower() in {"accept", "accept-language", "user-agent", "content-type", "range", "if-none-match", "if-modified-since"}
                       and isinstance(value, str) and len(value) <= 2048}
            headers["Accept-Encoding"] = "identity"
            try:
                # Browser redirects may bypass later route hooks. Fetch exactly
                # one validated origin through bounded, redirect-free transport,
                # then fulfill it; never continue a request to the browser stack.
                async with _credential_free_stream(client, route.request.method, target, headers) as response:
                    if 300 <= response.status_code < 400 and response.status_code != 304:
                        count("network_requests_blocked")
                        await route.fulfill(status=409, content_type="text/plain",
                                            body="Redirect blocked during local UI verification")
                        return
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError("Encoded resources are unsupported")
                    body = bytearray()

                    async def chunks():
                        if response.is_stream_consumed:
                            yield response.content
                        else:
                            async for chunk in response.aiter_raw(65536):
                                yield chunk

                    async for chunk in chunks():
                        report["resource_bytes"] += len(chunk)
                        if len(body) + len(chunk) > MAX_RESOURCE_BYTES or report["resource_bytes"] > MAX_RESPONSE_BYTES:
                            raise ValueError("UI resource budget exceeded")
                        body.extend(chunk)
                    response_headers = {key: value for key, value in response.headers.items()
                                        if key.lower() not in {"set-cookie", "location", "content-length", "content-encoding", "transfer-encoding"}}
                    await route.fulfill(status=response.status_code, headers=response_headers, body=bytes(body))
            except (httpx.HTTPError, ValueError):
                count("network_requests_blocked")
                await route.abort()

        async def deny_websocket(websocket):
            count("websockets_blocked")
            await websocket.close(code=1008, reason="UI verification disables WebSockets")

        try:
            try:
                factory = self.playwright_factory or _load_playwright_factory()
            except ImportError:
                raise VisualUnavailable("Install the optional Playwright package and Chromium before using UI verification") from None
            client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                       transport=self.http_transport, timeout=min(5, timeout_seconds))
            async with asyncio.timeout(request["timeout_seconds"]):
                playwright = await factory().start()
                browser = await playwright.chromium.launch(
                    headless=True, chromium_sandbox=True, timeout=min(10000, timeout_seconds * 1000),
                    args=["--disable-background-networking", "--no-proxy-server"],
                    env={key: value for key, value in os.environ.items()
                         if key in {"PATH", "HOME", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "XDG_RUNTIME_DIR"}},
                )
                context = await browser.new_context(
                    viewport=request["viewport"], service_workers="block", accept_downloads=False,
                    permissions=[], ignore_https_errors=False, java_script_enabled=True,
                )
                if not callable(getattr(context, "route_web_socket", None)):
                    raise VisualUnavailable("This Playwright version cannot intercept WebSockets; update the optional browser dependency")
                await context.route("**/*", restrict)
                await context.route_web_socket("**/*", deny_websocket)
                page = await context.new_page()
                page.on("console", lambda event: count("console_errors") if event.type == "error" else None)
                page.on("pageerror", lambda _event: count("page_errors"))
                navigation_started = True
                report["navigation_started"] = True
                response = await page.goto(request["url"], wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                report["http_status"] = response.status if response is not None else None
                for index, assertion in enumerate(request["assertions"]):
                    try:
                        locator = page.locator(assertion["selector"])
                        assertion_timeout = max(1, min(3000, int((timeout_seconds - (time.monotonic() - started)) * 1000)))
                        ready = locator.filter(has_text=assertion["text"]) if assertion["visible"] and "text" in assertion else locator
                        await ready.wait_for(state="visible" if assertion["visible"] else "hidden", timeout=assertion_timeout)
                        matches = await locator.count()
                        visible = bool(await locator.is_visible()) if matches == 1 else False
                        text_matches = True
                        if "text" in assertion:
                            text = await locator.evaluate("node => (node.innerText || '').slice(0, 4096)") if matches == 1 else ""
                            text_matches = isinstance(text, str) and assertion["text"] in text
                        passed = (matches <= 1 and visible == assertion["visible"] and text_matches)
                    except Exception:
                        visible, text_matches, passed = None, False, False
                    report["assertions"].append({"index": index, "passed": passed,
                                                  "visible": visible, "text_matches": text_matches})
                metrics = await page.evaluate("""() => ({width: document.documentElement.clientWidth,
                    scroll_width: document.documentElement.scrollWidth, height: document.documentElement.clientHeight,
                    scroll_height: document.documentElement.scrollHeight})""")
                if (not isinstance(metrics, dict) or set(metrics) != {"width", "scroll_width", "height", "scroll_height"}
                        or any(type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 10_000_000 for value in metrics.values())):
                    raise ValueError("Invalid browser layout measurements")
                report["layout"] = metrics
                report["horizontal_overflow"] = metrics["scroll_width"] > metrics["width"] + 1
                screenshot = await page.screenshot(type="png", full_page=False, animations="disabled", caret="hide")
                report["artifact"] = await asyncio.to_thread(_store_artifact, self.project_root, screenshot)
                healthy = (report["http_status"] is not None and 200 <= report["http_status"] < 400
                           and not report["page_errors"] and not report["console_errors"] and not report["horizontal_overflow"])
                passed = healthy and all(item["passed"] for item in report["assertions"])
                report["status"] = "verified" if passed and report["assertions"] else "captured" if passed else "failed"
                report["explanation"] = (
                    "Explicit UI assertions and basic page checks passed." if report["status"] == "verified"
                    else "Viewport captured; no explicit UI assertions were supplied." if report["status"] == "captured"
                    else "One or more observed UI assertions or basic page checks failed."
                )
        except VisualUnavailable as exc:
            report.update(status="unavailable", explanation=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            report.update(status="failed" if navigation_started else "unavailable",
                          explanation="Local browser verification failed." if navigation_started else "The optional local browser runtime could not start.")
        finally:
            failed_cleanup = False
            for owner in (context, browser, playwright, client):
                callback = getattr(owner, "close", None) or getattr(owner, "stop", None) or getattr(owner, "aclose", None)
                if callback is not None:
                    try:
                        await asyncio.wait_for(callback(), timeout=2)
                    except Exception:
                        failed_cleanup = True
            if failed_cleanup:
                report.update(cleanup_incomplete=True, status="failed",
                              explanation="Browser cleanup did not complete; inspect local browser processes.")
            report["elapsed_seconds"] = time.monotonic() - started
        return report

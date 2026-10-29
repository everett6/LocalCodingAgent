"""Local UI checks use a fake browser adapter, never external applications."""
import asyncio
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from local_coder.verification.visual import VisualVerifier, validate_visual_request
from local_coder.tools.visual import VisualVerificationTool


PNG = b"\x89PNG\r\n\x1a\n" + b"inert-browser-test-artifact"


class FakeLocator:
    def __init__(self, visible=True, text="Welcome to Clin"):
        self.visible, self.text = visible, text
        self.wait_for = AsyncMock()

    def filter(self, **kwargs):
        return self

    async def count(self):
        return 1

    async def is_visible(self):
        return self.visible

    async def evaluate(self, script, **kwargs):
        assert "4096" in script
        return self.text


class FakePage:
    def __init__(self):
        self.handlers = {}
        self.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        self.screenshot = AsyncMock(return_value=PNG)
        self.metrics = {"width": 1000, "scroll_width": 1000, "height": 800, "scroll_height": 900}
        self.locators = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def locator(self, selector):
        return self.locators.get(selector, FakeLocator())

    async def evaluate(self, script):
        assert "scrollWidth" in script
        return self.metrics


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.routes = []
        self.websockets = []
        self.close = AsyncMock()

    async def route(self, pattern, callback):
        self.routes.append((pattern, callback))

    async def route_web_socket(self, pattern, callback):
        self.websockets.append((pattern, callback))

    async def new_page(self):
        return self.page


class FakeBrowser:
    def __init__(self):
        self.context = FakeContext()
        self.new_context = AsyncMock(return_value=self.context)
        self.close = AsyncMock()


class FakeFactory:
    def __init__(self):
        self.browser = FakeBrowser()
        self.chromium = SimpleNamespace(launch=AsyncMock(return_value=self.browser))
        self.stop = AsyncMock()

    def __call__(self):
        return self

    async def start(self):
        return self


def check(tmp_path, factory, *, handler=None, **kwargs):
    transport = httpx.MockTransport(handler or (lambda request: httpx.Response(200, content=b"inert asset")))
    return asyncio.run(VisualVerifier(tmp_path, playwright_factory=factory, http_transport=transport).verify(
        "http://127.0.0.1:3000/app", **kwargs))


@pytest.mark.parametrize("url", [
    "https://example.com", "http://localhost:3000", "http://127.0.0.1.evil.test",
    "http://127.0.0.1:3000/?token=value", "http://user:password@127.0.0.1/",
    "file:///etc/passwd", "http://127.0.0.1/#route", "http://127.0.0.1:99999/",
    "http://2130706433/", "http://[::ffff:127.0.0.1]/", "http://127.0.0.1/\n",
])
def test_rejects_remote_ambiguous_and_credential_urls(url):
    with pytest.raises(ValueError):
        validate_visual_request(url)


def test_accepts_only_explicit_bounded_assertions_and_viewports():
    assert validate_visual_request("http://[::1]:3000/")["origin"] == "http://[::1]:3000"
    for values in ({"assertions": [{"selector": "h1", "script": "arbitrary()"}]},
                   {"assertions": [{"selector": "h1", "visible": 1}]},
                   {"assertions": [{"selector": "h1", "text": "x" * 513}]},
                   {"assertions": [{"selector": "h1"}] * 21},
                   {"viewport": {"width": 99999, "height": 700}},
                   {"timeout_seconds": True}, {"timeout_seconds": 61}):
        with pytest.raises(ValueError):
            validate_visual_request("http://127.0.0.1:3000", **values)


def test_fresh_browser_assertions_and_private_screenshot(tmp_path):
    factory = FakeFactory()
    report = check(tmp_path, factory, assertions=[{"selector": "h1", "text": "Clin"}])
    assert report["status"] == "verified"
    assert report["assertions"] == [{"index": 0, "passed": True, "visible": True, "text_matches": True}]
    assert report["vision_assessment"] == "not_run"
    artifact = report["artifact"]
    path = tmp_path / artifact["path"]
    assert path.read_bytes() == PNG
    assert artifact["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert artifact["bytes"] == len(PNG)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    options = factory.browser.new_context.await_args.kwargs
    assert options["service_workers"] == "block" and options["accept_downloads"] is False
    assert options["permissions"] == [] and options["ignore_https_errors"] is False
    assert factory.chromium.launch.await_args.kwargs["chromium_sandbox"] is True
    factory.browser.context.close.assert_awaited_once()
    factory.browser.close.assert_awaited_once()
    factory.stop.assert_awaited_once()
    assert factory.browser.context.page.goto.await_args.kwargs["wait_until"] == "domcontentloaded"


def test_capture_without_assertions_is_not_labeled_verified(tmp_path):
    report = check(tmp_path, FakeFactory())
    assert report["status"] == "captured" and report["assertions"] == []
    assert report["vision_assessment"] == "not_run"


def test_failed_assertion_and_overflow_are_observed(tmp_path):
    factory = FakeFactory()
    factory.browser.context.page.locators["h1"] = FakeLocator(visible=False, text="other")
    factory.browser.context.page.metrics["scroll_width"] = 1200
    report = check(tmp_path, factory, assertions=[{"selector": "h1", "text": "Clin"}])
    assert report["status"] == "failed"
    assert report["horizontal_overflow"] is True
    assert report["assertions"][0]["passed"] is False
    assert "other" not in str(report)


def test_routes_allow_only_same_origin_read_requests_and_block_websockets(tmp_path):
    factory = FakeFactory()
    context = factory.browser.context

    async def exercise(*args, **kwargs):
        callback = context.routes[0][1]
        for url, method, allowed in [
            ("http://127.0.0.1:3000/assets/main.js?v=1", "GET", True),
            ("http://127.0.0.1:3000/settings", "POST", False),
            ("http://127.0.0.1:3001/private", "GET", False),
            ("https://example.com/upload", "GET", False),
            ("file:///etc/passwd", "GET", False),
            ("http://127.0.0.1.evil.test:3000/", "GET", False),
        ]:
            route = SimpleNamespace(request=SimpleNamespace(url=url, method=method),
                                    continue_=AsyncMock(), abort=AsyncMock(), fulfill=AsyncMock())
            await callback(route)
            route.continue_.assert_not_awaited()
            assert route.fulfill.await_count == int(allowed)
            assert route.abort.await_count == int(not allowed)
        websocket = SimpleNamespace(close=AsyncMock())
        await context.websockets[0][1](websocket)
        websocket.close.assert_awaited_once()
        return SimpleNamespace(status=200)

    factory.browser.context.page.goto.side_effect = exercise
    report = check(tmp_path, factory)
    assert report["network_requests_blocked"] == 5


def test_redirect_is_not_followed_and_credentials_are_not_forwarded(tmp_path):
    factory = FakeFactory()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://example.com/private"})

    async def exercise(*args, **kwargs):
        route = SimpleNamespace(request=SimpleNamespace(
            url="http://127.0.0.1:3000/redirect", method="GET",
            headers={"Authorization": "Bearer synthetic", "Cookie": "private=value", "Accept": "text/html"}),
            continue_=AsyncMock(), abort=AsyncMock(), fulfill=AsyncMock())
        await factory.browser.context.routes[0][1](route)
        route.continue_.assert_not_awaited()
        assert route.fulfill.await_args.kwargs["status"] == 409
        return SimpleNamespace(status=200)

    factory.browser.context.page.goto.side_effect = exercise
    report = check(tmp_path, factory, handler=handler)
    assert report["network_requests_blocked"] == 1
    assert len(requests) == 1 and str(requests[0].url) == "http://127.0.0.1:3000/redirect"
    assert "authorization" not in requests[0].headers and "cookie" not in requests[0].headers


def test_resource_response_limits_abort_oversized_assets(tmp_path):
    factory = FakeFactory()

    async def exercise(*args, **kwargs):
        route = SimpleNamespace(request=SimpleNamespace(url="http://127.0.0.1:3000/large", method="GET"),
                                abort=AsyncMock(), fulfill=AsyncMock())
        await factory.browser.context.routes[0][1](route)
        route.abort.assert_awaited_once()
        route.fulfill.assert_not_awaited()
        return SimpleNamespace(status=200)

    factory.browser.context.page.goto.side_effect = exercise
    report = check(tmp_path, factory, handler=lambda request: httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1)))
    assert report["network_requests_blocked"] == 1


def test_resource_transport_does_not_replay_response_cookies(tmp_path):
    factory = FakeFactory()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"Set-Cookie": "private=synthetic; Path=/"}, content=b"ok")

    async def exercise(*args, **kwargs):
        for path in ("first", "second"):
            route = SimpleNamespace(request=SimpleNamespace(url=f"http://127.0.0.1:3000/{path}", method="GET"),
                                    abort=AsyncMock(), fulfill=AsyncMock())
            await factory.browser.context.routes[0][1](route)
            assert "set-cookie" not in route.fulfill.await_args.kwargs["headers"]
        return SimpleNamespace(status=200)

    factory.browser.context.page.goto.side_effect = exercise
    check(tmp_path, factory, handler=handler)
    assert len(requests) == 2 and all("cookie" not in request.headers for request in requests)


def test_old_browser_cannot_navigate_without_websocket_interception(tmp_path):
    factory = FakeFactory()
    factory.browser.context.route_web_socket = None
    report = check(tmp_path, factory)
    assert report["status"] == "unavailable"
    factory.browser.context.page.goto.assert_not_awaited()
    assert not list(tmp_path.rglob("*.png"))


def test_missing_optional_browser_support_is_unavailable(tmp_path, monkeypatch):
    def unavailable():
        raise ImportError("synthetic private installation path")
    monkeypatch.setattr("local_coder.verification.visual._load_playwright_factory", unavailable)
    report = asyncio.run(VisualVerifier(tmp_path).verify("http://127.0.0.1:3000"))
    assert report["status"] == "unavailable"
    assert "synthetic private" not in str(report)


def test_browser_faults_do_not_disclose_page_text_and_cleanup(tmp_path):
    factory = FakeFactory()
    factory.browser.context.page.goto.side_effect = RuntimeError("private diagnostic token=value")
    report = check(tmp_path, factory)
    assert report["status"] == "failed"
    assert "private diagnostic" not in str(report)
    factory.browser.close.assert_awaited_once()


def test_cancellation_closes_owned_browser(tmp_path):
    factory = FakeFactory()

    async def wait(*args, **kwargs):
        raise asyncio.CancelledError

    factory.browser.context.page.goto.side_effect = wait
    with pytest.raises(asyncio.CancelledError):
        check(tmp_path, factory)
    factory.browser.close.assert_awaited_once()
    factory.stop.assert_awaited_once()


def test_artifact_symlink_is_rejected_without_touching_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / ".local-coder"
    state.symlink_to(outside, target_is_directory=True)
    report = check(tmp_path, FakeFactory())
    assert report["status"] == "failed"
    assert not list(outside.rglob("*.png"))


def test_artifact_size_is_bounded(tmp_path):
    factory = FakeFactory()
    factory.browser.context.page.screenshot.return_value = PNG + b"x" * (8 * 1024 * 1024)
    report = check(tmp_path, factory)
    assert report["status"] == "failed"
    assert not list(tmp_path.rglob("*.png"))


def test_project_artifact_retention_has_a_fixed_count_limit(tmp_path):
    directory = tmp_path / ".local-coder" / "artifacts" / "ui"
    directory.mkdir(parents=True)
    for index in range(50):
        (directory / f"{index}.png").write_bytes(PNG)
    report = check(tmp_path, FakeFactory())
    assert report["status"] == "failed"
    assert len(list(directory.glob("*.png"))) == 50


def test_tool_requires_a_trusted_explicit_approval_callback(tmp_path):
    verifier = SimpleNamespace(verify=AsyncMock())
    result = asyncio.run(VisualVerificationTool(tmp_path, verifier=verifier).execute(
        url="http://127.0.0.1:3000"))
    assert not result.success and result.data["execution_outcome"] == "not_started"
    verifier.verify.assert_not_awaited()


@pytest.mark.parametrize("approved", [False, "yes", None])
def test_tool_denied_or_non_boolean_approval_never_navigates(tmp_path, approved):
    verifier = SimpleNamespace(verify=AsyncMock())
    callback = AsyncMock(return_value=approved)
    result = asyncio.run(VisualVerificationTool(tmp_path, approval_callback=callback, verifier=verifier).execute(
        url="http://127.0.0.1:3000", assertions=[{"selector": "h1"}]))
    assert not result.success
    verifier.verify.assert_not_awaited()
    callback.assert_awaited_once()


def test_tool_validates_before_approval_and_rejects_engine_fields(tmp_path):
    callback = AsyncMock(return_value=True)
    verifier = SimpleNamespace(verify=AsyncMock())
    tool = VisualVerificationTool(tmp_path, approval_callback=callback, verifier=verifier)
    for values in ({"url": "https://example.com"},
                   {"url": "http://127.0.0.1:3000", "approved": True},
                   {"url": "http://127.0.0.1:3000", "output_path": "outside.png"}):
        result = asyncio.run(tool.execute(**values))
        assert not result.success and result.data["execution_outcome"] == "not_started"
    callback.assert_not_awaited()
    verifier.verify.assert_not_awaited()


@pytest.mark.parametrize("status,success", [("verified", True), ("captured", True), ("failed", False), ("unavailable", False)])
def test_tool_preserves_honest_observed_status(tmp_path, status, success):
    report = {"status": status, "assertions": [], "artifact": None, "navigation_started": False,
              "explanation": "Observed test result", "vision_assessment": "not_run"}
    verifier = SimpleNamespace(verify=AsyncMock(return_value=report))
    tool = VisualVerificationTool(tmp_path, approval_callback=AsyncMock(return_value=True), verifier=verifier)
    result = asyncio.run(tool.execute(url="http://127.0.0.1:3000"))
    assert result.success is success
    assert result.data["visual_verification"] == report
    assert "Observed test result" in result.output

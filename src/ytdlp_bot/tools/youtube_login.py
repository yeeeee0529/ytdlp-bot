from __future__ import annotations

import argparse
import contextlib
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ytdlp_bot.tools.browser_detection import (
    BrowserCandidate,
    BrowserKind,
    browser_candidates,
)

YOUTUBE_URL = "https://www.youtube.com/"
COOKIE_URLS = (YOUTUBE_URL, "https://accounts.google.com/")
COOKIE_DOMAINS = ("youtube.com", "google.com")
AUTH_COOKIE_NAMES = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
    }
)
DEFAULT_OUTPUT = Path("secrets/youtube_cookies.txt")


class LoginToolError(RuntimeError):
    """Expected operator-facing login-tool failure."""


class PollableProcess(Protocol):
    def poll(self) -> int | None: ...


def build_native_browser_args(
    executable: Path,
    profile_dir: Path,
    *,
    debugging_port: int | None,
) -> list[str]:
    args = [
        str(executable),
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
    ]
    if debugging_port is not None:
        args.extend(
            [
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={debugging_port}",
            ]
        )
    args.append(YOUTUBE_URL)
    return args


def spawn_native_browser(
    candidate: BrowserCandidate,
    profile_dir: Path,
    *,
    debugging_port: int | None,
) -> subprocess.Popen[bytes]:
    if candidate.executable is None:
        raise LoginToolError("Browser executable is not available")
    return subprocess.Popen(
        build_native_browser_args(
            candidate.executable,
            profile_dir,
            debugging_port=debugging_port,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_browser(process: subprocess.Popen[bytes], timeout_seconds: float = 10) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)


def wait_for_enter_or_exit(
    process: PollableProcess,
    timeout_seconds: int,
    *,
    input_stream: Any = sys.stdin,
) -> None:
    _wait_for_confirmation(
        timeout_seconds,
        input_stream=input_stream,
        exited=lambda: process.poll() is not None,
    )


def _wait_for_confirmation(
    timeout_seconds: int,
    *,
    input_stream: Any,
    exited: Callable[[], bool] | None = None,
) -> None:
    results: queue.Queue[bool] = queue.Queue(maxsize=1)

    def read_enter() -> None:
        try:
            results.put(input_stream.readline() != "")
        except (OSError, ValueError):
            results.put(False)

    threading.Thread(target=read_enter, daemon=True).start()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if results.get_nowait():
                return
            raise LoginToolError("Interactive stdin closed before login confirmation")
        except queue.Empty:
            pass
        if exited is not None and exited():
            raise LoginToolError("Browser exited before login confirmation")
        time.sleep(0.1)
    raise LoginToolError("Timed out while waiting for login confirmation")


def get_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def wait_for_cdp(port: int, timeout_seconds: float = 15) -> None:
    deadline = time.monotonic() + timeout_seconds
    endpoint = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise LoginToolError("Timed out while waiting for the local CDP endpoint")


def is_supported_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").casefold()
    return any(
        normalized == allowed or normalized.endswith(f".{allowed}") for allowed in COOKIE_DOMAINS
    )


def has_youtube_auth_cookie(cookies: Iterable[Mapping[str, Any]]) -> bool:
    return any(
        cookie.get("name") in AUTH_COOKIE_NAMES
        and isinstance(cookie.get("value"), str)
        and bool(cookie["value"])
        and isinstance(cookie.get("domain"), str)
        and is_supported_cookie_domain(cookie["domain"])
        for cookie in cookies
    )


def cookies_to_netscape(cookies: Iterable[Mapping[str, Any]]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Generated by ytdlp-youtube-login", ""]
    for cookie in sorted(
        cookies,
        key=lambda item: (
            str(item.get("domain", "")),
            str(item.get("path", "")),
            str(item.get("name", "")),
        ),
    ):
        line = _cookie_to_netscape_line(cookie)
        if line is not None:
            lines.append(line)
    return "\n".join(lines) + "\n"


def _cookie_to_netscape_line(cookie: Mapping[str, Any]) -> str | None:
    domain = cookie.get("domain")
    path = cookie.get("path")
    name = cookie.get("name")
    value = cookie.get("value")
    if not all(isinstance(field, str) for field in (domain, path, name, value)):
        return None
    assert isinstance(domain, str)
    assert isinstance(path, str)
    assert isinstance(name, str)
    assert isinstance(value, str)
    if not domain or not path or not name or not is_supported_cookie_domain(domain):
        return None
    if any(
        "\t" in field or "\r" in field or "\n" in field for field in (domain, path, name, value)
    ):
        return None

    http_only = bool(cookie.get("httpOnly", False))
    output_domain = f"#HttpOnly_{domain}" if http_only else domain
    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
    secure = "TRUE" if bool(cookie.get("secure", False)) else "FALSE"
    expires_raw = cookie.get("expires", 0)
    try:
        expires = max(0, int(float(expires_raw)))
    except (TypeError, ValueError, OverflowError):
        expires = 0
    return "\t".join((output_domain, include_subdomains, path, secure, str(expires), name, value))


def atomic_write_cookie_file(output_path: Path, content: str) -> None:
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            dir=output_path.parent,
        )
        temporary_path = Path(raw_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def export_native_chromium_cookies(
    candidate: BrowserCandidate,
    profile_dir: Path,
    *,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    print(
        f"\n階段 1/2：以獨立暫存 profile 開啟 {candidate.label}。\n"
        "此階段不會啟用 remote debugging，也不會存取日常瀏覽器 profile。\n"
        "請在瀏覽器完成 Google / YouTube 登入（包含 2FA），確認已登入後回到此處按 Enter。\n"
    )
    login_process = spawn_native_browser(candidate, profile_dir, debugging_port=None)
    try:
        wait_for_enter_or_exit(login_process, timeout_seconds)
    finally:
        stop_browser(login_process)

    print("\n階段 2/2：短暫重新開啟相同暫存 profile，透過本機 CDP 匯出 Cookie。")
    port = get_free_loopback_port()
    export_process = spawn_native_browser(candidate, profile_dir, debugging_port=port)
    browser: Any = None
    try:
        wait_for_cdp(port)
        time.sleep(1)
        sync_playwright = _load_sync_playwright()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            contexts = browser.contexts
            if not contexts:
                raise LoginToolError("No browser context available over CDP")
            return list(contexts[0].cookies(list(COOKIE_URLS)))
    finally:
        stop_browser(export_process)
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()


def export_firefox_cookies(
    candidate: BrowserCandidate,
    profile_dir: Path,
    *,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    print(
        "\n將以 Playwright Firefox 與獨立暫存 profile 開啟 YouTube。\n"
        "請完成 Google / YouTube 登入（包含 2FA），確認已登入後回到此處按 Enter。\n"
    )
    sync_playwright = _load_sync_playwright()
    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {"headless": False}
        if candidate.executable is not None:
            launch_options["executable_path"] = str(candidate.executable)
        context = playwright.firefox.launch_persistent_context(
            str(profile_dir),
            **launch_options,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(YOUTUBE_URL, wait_until="domcontentloaded")
            _wait_for_confirmation(timeout_seconds, input_stream=sys.stdin)
            return list(context.cookies(list(COOKIE_URLS)))
        finally:
            with contextlib.suppress(Exception):
                context.close()


def _load_sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LoginToolError(
            "Playwright is not installed; run `uv sync --extra login` first"
        ) from exc
    return sync_playwright


def select_browser(requested: BrowserKind | None) -> BrowserCandidate:
    candidates = browser_candidates(requested=requested)
    if not candidates:
        choice = requested or "supported Chromium browser"
        raise LoginToolError(f"No executable found for {choice}")
    return candidates[0]


def run_login(
    *,
    requested_browser: BrowserKind | None,
    output_path: Path,
    timeout_seconds: int,
) -> None:
    candidate = select_browser(requested_browser)
    print(f"選用瀏覽器：{candidate.label}")
    with tempfile.TemporaryDirectory(prefix="ytdlp-youtube-login-") as raw_profile_dir:
        profile_dir = Path(raw_profile_dir)
        if candidate.native_chromium:
            cookies = export_native_chromium_cookies(
                candidate,
                profile_dir,
                timeout_seconds=timeout_seconds,
            )
        else:
            cookies = export_firefox_cookies(
                candidate,
                profile_dir,
                timeout_seconds=timeout_seconds,
            )

    if not has_youtube_auth_cookie(cookies):
        raise LoginToolError("No recognized Google or YouTube authentication cookie was exported")
    content = cookies_to_netscape(cookies)
    atomic_write_cookie_file(output_path, content)
    print(f"Cookie 檔已安全寫入：{output_path}（權限 0600）")
    print("請將此檔案視同帳號密碼管理，且不要提交到版本控制。")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ytdlp-youtube-login",
        description="以獨立暫存瀏覽器 profile 手動登入 YouTube，並匯出 Netscape Cookie 檔。",
    )
    parser.add_argument(
        "--browser",
        choices=("chrome", "edge", "brave", "firefox"),
        help="指定登入瀏覽器；預設先嘗試系統預設瀏覽器。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Cookie 輸出路徑（預設：{DEFAULT_OUTPUT}）。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="等待手動登入的秒數（預設：600）。",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_login(
            requested_browser=args.browser,
            output_path=args.output.expanduser().resolve(),
            timeout_seconds=args.timeout,
        )
    except KeyboardInterrupt:
        print("Error: Login cancelled by operator", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0

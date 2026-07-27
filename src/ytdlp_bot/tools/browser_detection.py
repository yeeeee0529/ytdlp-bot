from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BrowserKind = Literal["chrome", "edge", "brave", "firefox"]


@dataclass(frozen=True, slots=True)
class BrowserCandidate:
    kind: BrowserKind
    label: str
    executable: Path | None
    native_chromium: bool


_LABELS: dict[BrowserKind, str] = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "brave": "Brave Browser",
    "firefox": "Mozilla Firefox",
}


def map_default_browser_id(browser_id: str | None) -> BrowserKind | None:
    if browser_id is None:
        return None
    normalized = browser_id.casefold()
    if any(token in normalized for token in ("google.chrome", "google-chrome", "chromehtml")):
        return "chrome"
    if any(token in normalized for token in ("microsoft.edge", "microsoft-edge", "msedgehtm")):
        return "edge"
    if any(token in normalized for token in ("brave", "bravehtml")):
        return "brave"
    if any(token in normalized for token in ("firefox", "mozilla")):
        return "firefox"
    return None


def detect_default_browser_id(
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> str | None:
    current_platform = platform or sys.platform
    try:
        if current_platform == "darwin":
            return _detect_macos_default(home or Path.home())
        if current_platform == "win32":
            return _detect_windows_default()
        if current_platform.startswith("linux"):
            for command in (
                ["xdg-settings", "get", "default-web-browser"],
                ["xdg-mime", "query", "default", "x-scheme-handler/https"],
            ):
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                value = completed.stdout.strip()
                if completed.returncode == 0 and value:
                    return value
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return None
    return None


def _detect_macos_default(home: Path) -> str | None:
    path = (
        home
        / "Library"
        / "Preferences"
        / "com.apple.LaunchServices"
        / "com.apple.launchservices.secure.plist"
    )
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        data = plistlib.load(handle)
    chosen: str | None = None
    for handler in data.get("LSHandlers", []):
        if handler.get("LSHandlerURLScheme") != "https":
            continue
        role = handler.get("LSHandlerRoleAll") or handler.get("LSHandlerRoleViewer")
        if isinstance(role, str) and role:
            chosen = role
    return chosen


def _detect_windows_default() -> str | None:
    key_path = (
        r"HKCU\Software\Microsoft\Windows\Shell\Associations"
        r"\UrlAssociations\https\UserChoice"
    )
    completed = subprocess.run(
        ["reg", "query", key_path, "/v", "ProgId"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0].casefold() == "progid":
            return fields[-1]
    return None


def browser_candidates(
    *,
    requested: BrowserKind | None = None,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[BrowserCandidate]:
    current_platform = platform or sys.platform
    current_environ = environ if environ is not None else os.environ
    mapped_default = map_default_browser_id(
        detect_default_browser_id(platform=current_platform, home=home)
    )
    preferred = requested or (mapped_default if mapped_default != "firefox" else None)
    order: list[BrowserKind] = []
    choices: tuple[BrowserKind | None, ...]
    if requested is not None:
        choices = (requested,)
    else:
        choices = (preferred, "chrome", "edge", "brave", "firefox")
    for kind in choices:
        if kind is not None and kind not in order:
            order.append(kind)

    candidates: list[BrowserCandidate] = []
    for kind in order:
        executable = resolve_browser_executable(
            kind,
            platform=current_platform,
            environ=current_environ,
        )
        if executable is not None:
            candidates.append(
                BrowserCandidate(
                    kind=kind,
                    label=_LABELS[kind],
                    executable=executable,
                    native_chromium=kind != "firefox",
                )
            )

    if (requested == "firefox" or requested is None) and not any(
        candidate.kind == "firefox" for candidate in candidates
    ):
        candidates.append(
            BrowserCandidate(
                kind="firefox",
                label="Playwright Firefox",
                executable=None,
                native_chromium=False,
            )
        )
    return candidates


def resolve_browser_executable(
    kind: BrowserKind,
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    current_platform = platform or sys.platform
    current_environ = environ if environ is not None else os.environ
    paths = _browser_paths(kind, current_platform, current_environ)
    for path in paths:
        if path.is_file():
            return path
    for command in _browser_commands(kind):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved)
    return None


def _browser_paths(
    kind: BrowserKind,
    platform: str,
    environ: Mapping[str, str],
) -> list[Path]:
    if platform == "darwin":
        application_names = {
            "chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
            "edge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "brave": "Brave Browser.app/Contents/MacOS/Brave Browser",
            "firefox": "Firefox.app/Contents/MacOS/firefox",
        }
        return [Path("/Applications") / application_names[kind]]

    if platform == "win32":
        program_files = Path(environ.get("ProgramFiles", r"C:\Program Files"))
        program_files_x86 = Path(environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        local_app_data_raw = environ.get("LOCALAPPDATA")
        local_app_data = Path(local_app_data_raw) if local_app_data_raw else None
        suffixes: dict[BrowserKind, tuple[str, ...]] = {
            "chrome": ("Google", "Chrome", "Application", "chrome.exe"),
            "edge": ("Microsoft", "Edge", "Application", "msedge.exe"),
            "brave": ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            "firefox": ("Mozilla Firefox", "firefox.exe"),
        }
        roots = [program_files, program_files_x86]
        if local_app_data is not None:
            roots.append(local_app_data)
        return [root.joinpath(*suffixes[kind]) for root in roots]

    linux_paths: dict[BrowserKind, tuple[str, ...]] = {
        "chrome": (
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/snap/bin/chromium",
        ),
        "edge": ("/usr/bin/microsoft-edge-stable", "/usr/bin/microsoft-edge"),
        "brave": ("/usr/bin/brave-browser", "/usr/bin/brave-browser-stable"),
        "firefox": ("/usr/bin/firefox", "/snap/bin/firefox"),
    }
    return [Path(path) for path in linux_paths[kind]]


def _browser_commands(kind: BrowserKind) -> tuple[str, ...]:
    commands: dict[BrowserKind, tuple[str, ...]] = {
        "chrome": ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"),
        "edge": ("microsoft-edge-stable", "microsoft-edge"),
        "brave": ("brave-browser", "brave-browser-stable"),
        "firefox": ("firefox",),
    }
    return commands[kind]

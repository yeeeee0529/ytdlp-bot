from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from ytdlp_bot.tools import browser_detection
from ytdlp_bot.tools.browser_detection import (
    BrowserCandidate,
    browser_candidates,
    detect_default_browser_id,
    map_default_browser_id,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("browser_id", "expected"),
    [
        ("com.google.Chrome", "chrome"),
        ("ChromeHTML", "chrome"),
        ("com.microsoft.edgemac", "edge"),
        ("MSEdgeHTM", "edge"),
        ("com.brave.Browser", "brave"),
        ("firefox.desktop", "firefox"),
        ("com.apple.Safari", None),
        (None, None),
    ],
)
def test_map_default_browser_id(browser_id: str | None, expected: str | None) -> None:
    assert map_default_browser_id(browser_id) == expected


def test_detect_macos_default_browser_from_launch_services(tmp_path: Path) -> None:
    preferences = (
        tmp_path
        / "Library"
        / "Preferences"
        / "com.apple.LaunchServices"
        / "com.apple.launchservices.secure.plist"
    )
    preferences.parent.mkdir(parents=True)
    with preferences.open("wb") as handle:
        plistlib.dump(
            {
                "LSHandlers": [
                    {
                        "LSHandlerURLScheme": "https",
                        "LSHandlerRoleAll": "com.google.Chrome",
                    }
                ]
            },
            handle,
        )

    assert detect_default_browser_id(platform="darwin", home=tmp_path) == "com.google.Chrome"


def test_browser_candidates_put_supported_default_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_detection,
        "detect_default_browser_id",
        lambda **_kwargs: "com.brave.Browser",
    )
    resolved = {
        "chrome": Path("/browser/chrome"),
        "edge": Path("/browser/edge"),
        "brave": Path("/browser/brave"),
        "firefox": None,
    }
    monkeypatch.setattr(
        browser_detection,
        "resolve_browser_executable",
        lambda kind, **_kwargs: resolved[kind],
    )

    candidates = browser_candidates(platform="darwin")

    assert [candidate.kind for candidate in candidates] == [
        "brave",
        "chrome",
        "edge",
        "firefox",
    ]
    assert candidates[-1] == BrowserCandidate(
        kind="firefox",
        label="Playwright Firefox",
        executable=None,
        native_chromium=False,
    )


def test_firefox_default_remains_after_native_chromium_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_detection,
        "detect_default_browser_id",
        lambda **_kwargs: "firefox.desktop",
    )
    monkeypatch.setattr(
        browser_detection,
        "resolve_browser_executable",
        lambda kind, **_kwargs: Path(f"/browser/{kind}"),
    )

    assert [candidate.kind for candidate in browser_candidates(platform="linux")] == [
        "chrome",
        "edge",
        "brave",
        "firefox",
    ]


def test_linux_default_detection_falls_back_to_xdg_mime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        [
            browser_detection.subprocess.CompletedProcess([], 1, "", ""),
            browser_detection.subprocess.CompletedProcess([], 0, "brave-browser.desktop\n", ""),
        ]
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> object:
        commands.append(command)
        return next(results)

    monkeypatch.setattr(browser_detection.subprocess, "run", fake_run)

    assert detect_default_browser_id(platform="linux") == "brave-browser.desktop"
    assert commands == [
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "x-scheme-handler/https"],
    ]


def test_requested_browser_does_not_silently_select_another_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_detection,
        "resolve_browser_executable",
        lambda kind, **_kwargs: Path("/browser/edge") if kind == "edge" else None,
    )

    assert browser_candidates(requested="chrome", platform="linux") == []


def test_requested_firefox_allows_playwright_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_detection,
        "resolve_browser_executable",
        lambda _kind, **_kwargs: None,
    )

    assert browser_candidates(requested="firefox", platform="linux") == [
        BrowserCandidate(
            kind="firefox",
            label="Playwright Firefox",
            executable=None,
            native_chromium=False,
        )
    ]

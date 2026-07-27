from __future__ import annotations

import io
import stat
from pathlib import Path
from typing import Any

import pytest

from ytdlp_bot.tools import youtube_login
from ytdlp_bot.tools.browser_detection import BrowserCandidate
from ytdlp_bot.tools.youtube_login import (
    LoginToolError,
    atomic_write_cookie_file,
    build_native_browser_args,
    cookies_to_netscape,
    export_native_chromium_cookies,
    has_youtube_auth_cookie,
    main,
    run_login,
    wait_for_enter_or_exit,
)

pytestmark = pytest.mark.unit


def _candidate() -> BrowserCandidate:
    return BrowserCandidate(
        kind="chrome",
        label="Google Chrome",
        executable=Path("/Applications/Google Chrome"),
        native_chromium=True,
    )


def _auth_cookie(value: str = "secret-cookie-value") -> dict[str, Any]:
    return {
        "name": "SAPISID",
        "value": value,
        "domain": ".youtube.com",
        "path": "/",
        "expires": 1_900_000_000.25,
        "httpOnly": True,
        "secure": True,
    }


def test_phase_one_native_args_have_no_debugging_or_automation_flags() -> None:
    args = build_native_browser_args(
        Path("/browser/chrome"),
        Path("/tmp/isolated-profile"),
        debugging_port=None,
    )

    assert not any("remote-debugging" in arg for arg in args)
    assert not any("automation" in arg.casefold() for arg in args)
    assert "--user-data-dir=/tmp/isolated-profile" in args


def test_phase_two_native_args_bind_remote_debugging_to_loopback() -> None:
    args = build_native_browser_args(
        Path("/browser/chrome"),
        Path("/tmp/isolated-profile"),
        debugging_port=9222,
    )

    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-debugging-port=9222" in args


def test_cookies_to_netscape_filters_domains_and_rejects_line_injection() -> None:
    content = cookies_to_netscape(
        [
            _auth_cookie(),
            {
                "name": "ignored",
                "value": "not-exported",
                "domain": ".example.com",
                "path": "/",
            },
            {
                "name": "bad",
                "value": "line\ninjection",
                "domain": ".youtube.com",
                "path": "/",
            },
        ]
    )

    assert content.startswith("# Netscape HTTP Cookie File\n")
    assert (
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1900000000\tSAPISID\tsecret-cookie-value\n"
    ) in content
    assert "not-exported" not in content
    assert "line\ninjection" not in content


def test_auth_cookie_detection_requires_known_name_domain_and_value() -> None:
    assert has_youtube_auth_cookie([_auth_cookie()])
    assert not has_youtube_auth_cookie([{**_auth_cookie(), "name": "PREF"}])
    assert not has_youtube_auth_cookie([{**_auth_cookie(), "domain": ".example.com"}])
    assert not has_youtube_auth_cookie([{**_auth_cookie(), "value": ""}])


def test_atomic_write_uses_mode_0600_and_replaces_existing_file(tmp_path: Path) -> None:
    output = tmp_path / "secrets" / "youtube_cookies.txt"
    output.parent.mkdir()
    output.write_text("old", encoding="utf-8")
    output.chmod(0o644)

    atomic_write_cookie_file(output, "new")

    assert output.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert list(output.parent.glob(f".{output.name}.*")) == []


def test_wait_for_enter_reports_closed_stdin_without_echoing_input() -> None:
    class RunningProcess:
        @staticmethod
        def poll() -> None:
            return None

    with pytest.raises(LoginToolError, match="stdin closed"):
        wait_for_enter_or_exit(
            RunningProcess(),
            1,
            input_stream=io.StringIO(""),
        )


def test_run_login_cleans_profile_and_does_not_print_cookie_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured_profile: Path | None = None
    cookie_value = "CANARY-COOKIE-VALUE"

    def fake_export(
        _candidate_arg: BrowserCandidate,
        profile_dir: Path,
        *,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        nonlocal captured_profile
        assert timeout_seconds == 30
        captured_profile = profile_dir
        return [_auth_cookie(cookie_value)]

    monkeypatch.setattr(youtube_login, "select_browser", lambda _requested: _candidate())
    monkeypatch.setattr(youtube_login, "export_native_chromium_cookies", fake_export)
    output = tmp_path / "youtube_cookies.txt"

    run_login(requested_browser=None, output_path=output, timeout_seconds=30)

    assert captured_profile is not None
    assert not captured_profile.exists()
    assert cookie_value not in capsys.readouterr().out
    assert cookie_value in output.read_text(encoding="utf-8")


def test_run_login_preserves_existing_output_when_auth_cookie_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(youtube_login, "select_browser", lambda _requested: _candidate())
    monkeypatch.setattr(
        youtube_login,
        "export_native_chromium_cookies",
        lambda *_args, **_kwargs: [
            {
                "name": "PREF",
                "value": "not-secret",
                "domain": ".youtube.com",
                "path": "/",
            }
        ],
    )
    output = tmp_path / "youtube_cookies.txt"
    output.write_text("existing", encoding="utf-8")

    with pytest.raises(LoginToolError, match="No recognized"):
        run_login(requested_browser=None, output_path=output, timeout_seconds=30)

    assert output.read_text(encoding="utf-8") == "existing"


def test_native_flow_reuses_profile_and_cleans_both_browser_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spawned: list[tuple[Path, int | None, object]] = []
    stopped: list[object] = []

    def fake_spawn(
        _candidate_arg: BrowserCandidate,
        profile_dir: Path,
        *,
        debugging_port: int | None,
    ) -> object:
        process = object()
        spawned.append((profile_dir, debugging_port, process))
        return process

    monkeypatch.setattr(youtube_login, "spawn_native_browser", fake_spawn)
    monkeypatch.setattr(youtube_login, "wait_for_enter_or_exit", lambda *_args: None)
    monkeypatch.setattr(youtube_login, "get_free_loopback_port", lambda: 9222)
    monkeypatch.setattr(
        youtube_login,
        "wait_for_cdp",
        lambda _port: (_ for _ in ()).throw(LoginToolError("CDP unavailable")),
    )
    monkeypatch.setattr(
        youtube_login,
        "stop_browser",
        lambda process: stopped.append(process),
    )

    with pytest.raises(LoginToolError, match="CDP unavailable"):
        export_native_chromium_cookies(
            _candidate(),
            tmp_path,
            timeout_seconds=30,
        )

    assert [(profile, port) for profile, port, _process in spawned] == [
        (tmp_path, None),
        (tmp_path, 9222),
    ]
    assert stopped == [spawned[0][2], spawned[1][2]]


def test_main_redacts_cookie_value_from_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cookie_value = "CANARY-COOKIE-VALUE"
    monkeypatch.setattr(
        youtube_login,
        "run_login",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("browser launch failed")),
    )

    assert main(["--timeout", "1"]) == 1

    captured = capsys.readouterr()
    assert "browser launch failed" in captured.err
    assert cookie_value not in captured.out
    assert cookie_value not in captured.err

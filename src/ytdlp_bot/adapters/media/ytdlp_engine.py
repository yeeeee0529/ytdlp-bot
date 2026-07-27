"""yt-dlp options builder (no network in unit tests)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ytdlp_bot.domain.enums import FailureCode, MediaMode
from ytdlp_bot.domain.format_policy import (
    FormatSelection,
    build_format_selection,
    sanitize_format_metadata,
)

# Options that must never appear from user input.
_FORBIDDEN_OPTION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "cookiefile",
        "cookiesfrombrowser",
        "username",
        "password",
        "netrc",
        "netrc_location",
        "config_locations",
        "plugin_dirs",
        "external_downloader",
        "external_downloader_args",
        "downloader",
        "update",
        "update_to",
        "js_runtimes",
        "remote_components",
    }
)
_COOKIE_FILE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "# HTTP Cookie File",
        "# Netscape HTTP Cookie File",
    }
)
_HTTPONLY_PREFIX: Final[str] = "#HttpOnly_"


@dataclass(frozen=True, slots=True)
class YtdlpOptions:
    """Trusted options dict for yt-dlp Python API."""

    raw: dict[str, Any]
    cookie_file: str | None = None

    def assert_allowlisted(self) -> None:
        for key in self.raw:
            if key in _FORBIDDEN_OPTION_KEYS:
                raise ValueError(f"forbidden yt-dlp option: {key}")
        if self.cookie_file is not None:
            if not isinstance(self.cookie_file, str) or not self.cookie_file:
                raise ValueError("cookie file path must be a non-empty string")
            if not Path(self.cookie_file).is_absolute():
                raise ValueError("cookie file path must be absolute")

    def materialize(self, *, include_cookie: bool = True) -> dict[str, Any]:
        """Build the final yt-dlp options from trusted structured fields."""
        self.assert_allowlisted()
        opts = dict(self.raw)
        if include_cookie and self.cookie_file is not None:
            opts["cookiefile"] = self.cookie_file
        return opts


def validate_cookie_file(cookie_file: str) -> None:
    """Validate Netscape cookie structure without exposing line contents."""
    try:
        with Path(cookie_file).open(encoding="utf-8") as handle:
            header = handle.readline().rstrip("\r\n")
            if header not in _COOKIE_FILE_HEADERS:
                raise ValueError("cookie file must use Netscape format")
            for line in handle:
                candidate = line.rstrip("\r\n")
                if candidate.startswith(_HTTPONLY_PREFIX):
                    candidate = candidate[len(_HTTPONLY_PREFIX) :]
                elif not candidate or candidate.startswith("#"):
                    continue
                fields = candidate.split("\t")
                if len(fields) != 7:
                    raise ValueError("cookie file contains an invalid entry")
                expires = fields[4]
                if expires and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", expires) is None:
                    raise ValueError("cookie file contains an invalid expiry")
    except ValueError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ValueError("cookie file could not be read") from exc


def build_ytdlp_options(
    selection: FormatSelection,
    *,
    workspace: str,
    proxy_url: str | None,
    network_attempts: int,
    outtmpl: str,
    cookie_file: str | None = None,
) -> YtdlpOptions:
    """Build a locked-down yt-dlp option dictionary."""
    attempts = max(1, min(int(network_attempts), 3))
    opts: dict[str, Any] = {
        "format": selection.format_string,
        "outtmpl": outtmpl,
        "noplaylist": selection.mode is MediaMode.VIDEO,
        "quiet": True,
        "no_warnings": True,
        "retries": attempts,
        "fragment_retries": attempts,
        "concurrent_fragment_downloads": 1,
        "paths": {"home": workspace},
        # Security lockdowns
        "no_color": True,
        "ignoreconfig": True,
        "cachedir": False,
        "geo_bypass": False,
        "prefer_insecure": False,
        "check_formats": False,
        "overwrites": True,
        "noprogress": True,
        "simulate": False,
        "skip_download": False,
        "writeinfojson": False,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        " syn_playlist": False,
        "extract_flat": False,
        "forceprint": {},
        "print_to_file": {},
    }
    if selection.merge_output_format:
        opts["merge_output_format"] = selection.merge_output_format
    if selection.postprocessors:
        opts["postprocessors"] = list(selection.postprocessors)
    if proxy_url:
        opts["proxy"] = proxy_url
    # Never pass arbitrary user flags.
    result = YtdlpOptions(raw=opts, cookie_file=cookie_file)
    result.assert_allowlisted()
    return result


def options_for_request(
    *,
    mode: MediaMode,
    quality: object | None,
    bitrate: object | None,
    workspace: str,
    proxy_url: str | None,
    network_attempts: int,
    cookie_file: str | None = None,
) -> YtdlpOptions:
    from ytdlp_bot.domain.enums import AudioBitrate, VideoQuality

    sel = build_format_selection(
        mode,
        quality=quality if isinstance(quality, VideoQuality) else None,
        bitrate=bitrate if isinstance(bitrate, AudioBitrate) else None,
    )
    # Prefer original title for the workspace filename; display_name is also
    # derived from metadata after download for Telegram / signed URLs.
    tmpl = str(Path(workspace) / "%(title).180B.%(ext)s")
    return build_ytdlp_options(
        sel,
        workspace=workspace,
        proxy_url=proxy_url,
        network_attempts=network_attempts,
        outtmpl=tmpl,
        cookie_file=cookie_file,
    )


def inspect_metadata_fixture(info: dict[str, Any]) -> dict[str, Any]:
    """Sanitize yt-dlp info dict into bounded safe metadata (no download)."""
    formats_raw = info.get("formats") or []
    formats = []
    if isinstance(formats_raw, list):
        for item in formats_raw[:200]:
            if isinstance(item, dict):
                formats.append(sanitize_format_metadata(item))
    title = str(info.get("title") or "")[:200]
    is_playlist = bool(info.get("entries")) or str(info.get("_type") or "") == "playlist"
    entry_count = None
    if is_playlist and isinstance(info.get("entries"), list):
        entry_count = min(len(info["entries"]), 10_000)
    return {
        "title": title,
        "is_playlist": is_playlist,
        "entry_count": entry_count,
        "formats": formats,
        "extractor": str(info.get("extractor") or info.get("ie_key") or "")[:64],
    }


def progress_hook_to_event(status: dict[str, Any], *, sequence: int) -> dict[str, Any]:
    """Translate yt-dlp progress hook dict into bounded event payload."""
    downloaded = status.get("downloaded_bytes")
    total = status.get("total_bytes") or status.get("total_bytes_estimate")
    speed = status.get("speed")
    eta = status.get("eta")

    def _int(v: object) -> int | None:
        if v is None:
            return None
        try:
            n = int(float(v))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if n < 0 or n > 2**63 - 1:
            return None
        return n

    return {
        "type": "progress_changed",
        "sequence": sequence,
        "payload": {
            "downloaded_bytes": _int(downloaded),
            "total_bytes": _int(total),
            "speed": _int(speed),
            "eta": _int(eta),
            "status": str(status.get("status") or "")[:32],
        },
    }


def classify_ytdlp_error(message: str) -> str:
    """Map extractor/network errors to stable codes (no raw message leakage)."""
    lower = message.lower()
    if (
        "sign in" in lower
        or "login" in lower
        or "authentication" in lower
        or "private video" in lower
        or "members-only" in lower
        or "confirm you're not a bot" in lower
    ):
        return FailureCode.AUTHENTICATION_REQUIRED.value
    if "drm" in lower or "protected" in lower:
        return FailureCode.DRM_UNSUPPORTED.value
    if (
        "requested format is not available" in lower
        or "only images are available" in lower
        or "no video formats found" in lower
    ):
        return FailureCode.NO_MATCHING_FORMAT.value
    if "unsupported url" in lower or "no suitable" in lower:
        return FailureCode.UNSUPPORTED_SOURCE.value
    if "private" in lower or "unavailable" in lower:
        return FailureCode.UNSUPPORTED_SOURCE.value
    if "network" in lower or "timeout" in lower or "connection" in lower:
        return FailureCode.DOWNLOAD_FAILED.value
    return FailureCode.DOWNLOAD_FAILED.value


def _extract_info(
    source_url: str,
    *,
    options: YtdlpOptions,
    workspace: Path,
    include_cookie: bool,
) -> object:
    """執行一次 yt-dlp, 並維持營運者 Cookie 檔案不可變."""
    from yt_dlp import YoutubeDL  # type: ignore[import-untyped]

    opts = options.materialize(include_cookie=include_cookie)
    opts["paths"] = {"home": str(workspace)}
    with YoutubeDL(opts) as ydl:
        try:
            return ydl.extract_info(source_url, download=True)
        finally:
            # yt-dlp 關閉時通常會把記憶體中的 Cookie jar 寫回檔案;
            # 營運者管理的 secret 會由多個 worker 共用, 因此必須維持唯讀.
            if include_cookie:
                ydl.params.pop("cookiefile", None)


def run_ytdlp_download(
    source_url: str,
    options: YtdlpOptions,
    *,
    workspace: Path,
) -> tuple[Path, str]:
    """Invoke pinned yt-dlp Python API; return (primary path, original title)."""
    from yt_dlp.utils import DownloadError  # type: ignore[import-untyped]

    options.assert_allowlisted()
    workspace.mkdir(parents=True, exist_ok=True)
    before = {p.resolve() for p in workspace.rglob("*") if p.is_file()}
    try:
        # 公開媒體先以匿名方式下載, 避免消耗營運者帳號 session,
        # 也避免受到僅限 Cookie client 的格式限制。
        extracted = _extract_info(
            source_url,
            options=options,
            workspace=workspace,
            include_cookie=False,
        )
    except Exception as anonymous_error:
        if (
            options.cookie_file is None
            or not isinstance(anonymous_error, DownloadError)
            or classify_ytdlp_error(str(anonymous_error))
            != FailureCode.AUTHENTICATION_REQUIRED.value
        ):
            raise
        validate_cookie_file(options.cookie_file)
        extracted = _extract_info(
            source_url,
            options=options,
            workspace=workspace,
            include_cookie=True,
        )
    after = [p for p in workspace.rglob("*") if p.is_file() and p.resolve() not in before]
    if not after:
        # Fallback: any media-like file in workspace.
        after = [
            p
            for p in workspace.rglob("*")
            if p.is_file() and p.suffix.lower() in {".mp4", ".mp3", ".m4a", ".webm", ".mkv"}
        ]
    if not after:
        raise RuntimeError("yt-dlp produced no output file")
    after.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    # Containment: reject anything outside workspace.
    root = workspace.resolve()
    primary = after[0].resolve()
    if root not in primary.parents and primary != root:
        raise RuntimeError("yt-dlp output escaped workspace")
    title = ""
    if isinstance(extracted, dict):
        title = str(extracted.get("title") or extracted.get("fulltitle") or "").strip()
        # Single-entry playlist wrappers occasionally nest the real entry.
        entries = extracted.get("entries")
        if not title and isinstance(entries, list) and entries:
            first = entries[0]
            if isinstance(first, dict):
                title = str(first.get("title") or first.get("fulltitle") or "").strip()
    return after[0], title

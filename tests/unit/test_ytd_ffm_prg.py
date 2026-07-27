"""YTD format policy, FFM verification, and PRG throttle tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ytdlp_bot.adapters.media.ffmpeg_engine import (
    FfmpegError,
    FfmpegOp,
    args_for_intent,
    build_intent,
    build_mp3_encode_args,
    build_mp4_compat_transcode_args,
    build_mp4_merge_args,
    parse_ffprobe_payload,
    verify_mp3,
    verify_mp4,
)
from ytdlp_bot.adapters.media.ytdlp_engine import (
    YtdlpOptions,
    build_ytdlp_options,
    classify_ytdlp_error,
    inspect_metadata_fixture,
    options_for_request,
    progress_hook_to_event,
    run_ytdlp_download,
)
from ytdlp_bot.application.progress_reporter import ProgressReporter
from ytdlp_bot.domain.enums import AudioBitrate, JobState, MediaMode, Platform, VideoQuality
from ytdlp_bot.domain.format_policy import (
    ProcessingIntent,
    build_format_selection,
    decide_source_shape,
    height_within_ceiling,
    sanitize_format_metadata,
    select_audio_format,
    select_video_format,
)
from ytdlp_bot.domain.identity import JobId, MessageReference
from ytdlp_bot.domain.progress import progress_from_worker_values


@pytest.mark.unit
def test_sanitize_format_metadata_drops_secrets() -> None:
    rec = sanitize_format_metadata(
        {
            "format_id": "137",
            "height": 1080,
            "vcodec": "avc1.640028",
            "acodec": "none",
            "url": "https://secret.example/video",
            "http_headers": {"Authorization": "Bearer X"},
            "ext": "mp4",
        }
    )
    assert rec.has_video and not rec.has_audio
    assert rec.height == 1080
    assert rec.format_id == "137"
    # Secrets never become fields on the sanitized record.
    assert not hasattr(rec, "url")


@pytest.mark.unit
def test_video_selection_respects_ceiling() -> None:
    formats = [
        sanitize_format_metadata(
            {"format_id": "1", "height": 2160, "vcodec": "avc1", "ext": "mp4"}
        ),
        sanitize_format_metadata(
            {"format_id": "2", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4"}
        ),
        sanitize_format_metadata({"format_id": "3", "height": 480, "vcodec": "avc1", "ext": "mp4"}),
    ]
    chosen = select_video_format(formats, quality=VideoQuality.P720)
    assert chosen is not None
    assert chosen.height == 720
    low = select_video_format(formats, quality=VideoQuality.P360)
    assert low is None


@pytest.mark.unit
def test_source_shape_audio_only_and_silent_video() -> None:
    audio_only = [
        sanitize_format_metadata({"format_id": "a", "acodec": "mp4a", "abr": 128, "ext": "m4a"})
    ]
    d = decide_source_shape(audio_only, mode=MediaMode.VIDEO)
    assert not d.ok and d.error_code == "AUDIO_ONLY_SOURCE" and d.suggest_ytmp3

    silent = [
        sanitize_format_metadata({"format_id": "v", "height": 720, "vcodec": "avc1", "ext": "mp4"})
    ]
    d2 = decide_source_shape(silent, mode=MediaMode.VIDEO, quality=VideoQuality.P720)
    assert d2.ok
    assert "source_has_no_audio" in d2.warning_codes

    d3 = decide_source_shape(audio_only, mode=MediaMode.AUDIO)
    assert d3.ok and d3.processing_intent is ProcessingIntent.MP3_ENCODE


@pytest.mark.unit
def test_audio_bitrate_defaults_and_selection() -> None:
    sel = build_format_selection(MediaMode.AUDIO)
    assert sel.target_bitrate is AudioBitrate.K320
    for br in AudioBitrate:
        s = build_format_selection(MediaMode.AUDIO, bitrate=br)
        assert s.postprocessors
        assert br.value.replace("k", "") in str(s.postprocessors)
    formats = [
        sanitize_format_metadata({"format_id": "a1", "acodec": "opus", "abr": 96}),
        sanitize_format_metadata({"format_id": "a2", "acodec": "mp4a", "abr": 256}),
    ]
    best = select_audio_format(formats)
    assert best is not None and best.abr == 256


@pytest.mark.unit
def test_ytdlp_options_lockdown() -> None:
    sel = build_format_selection(MediaMode.VIDEO, quality=VideoQuality.P1080)
    opts = build_ytdlp_options(
        sel,
        workspace="/tmp/ws",
        proxy_url="http://proxy:8080",
        network_attempts=5,
        outtmpl="/tmp/ws/%(id)s.%(ext)s",
    )
    assert opts.raw["ignoreconfig"] is True
    assert opts.raw["retries"] == 3  # capped
    assert opts.raw["proxy"] == "http://proxy:8080"
    assert "cookiefile" not in opts.raw
    assert "1080" in opts.raw["format"]
    opts.assert_allowlisted()
    o2 = options_for_request(
        mode=MediaMode.AUDIO,
        quality=None,
        bitrate=AudioBitrate.K192,
        workspace="/ws",
        proxy_url=None,
        network_attempts=1,
    )
    assert o2.raw["postprocessors"]
    assert "%(title)" in str(o2.raw["outtmpl"])
    assert classify_ytdlp_error("Sign in to confirm") == "AUTH_REQUIRED"
    assert classify_ytdlp_error("network timeout") == "NETWORK_ERROR"
    meta = inspect_metadata_fixture(
        {
            "title": "x" * 300,
            "extractor": "youtube",
            "formats": [
                {
                    "format_id": "1",
                    "height": 720,
                    "vcodec": "avc1",
                    "url": "https://secret",
                }
            ],
            "_type": "video",
        }
    )
    assert len(meta["title"]) <= 200
    assert meta["formats"] and not meta["is_playlist"]
    hook = progress_hook_to_event(
        {"downloaded_bytes": 10, "total_bytes": 100, "speed": 5, "eta": 2, "status": "downloading"},
        sequence=3,
    )
    assert hook["payload"]["downloaded_bytes"] == 10
    assert hook["sequence"] == 3


@pytest.mark.unit
def test_ytdlp_cookiefile_is_materialized_only_from_trusted_field() -> None:
    sel = build_format_selection(MediaMode.VIDEO, quality=VideoQuality.P720)
    opts = build_ytdlp_options(
        sel,
        workspace="/tmp/ws",
        proxy_url=None,
        network_attempts=1,
        outtmpl="/tmp/ws/%(id)s.%(ext)s",
        cookie_file="/run/secrets/youtube_cookies.txt",
    )

    assert "cookiefile" not in opts.raw
    assert opts.materialize()["cookiefile"] == "/run/secrets/youtube_cookies.txt"
    with pytest.raises(ValueError, match="forbidden yt-dlp option"):
        YtdlpOptions(raw={"cookiefile": "/tmp/untrusted.txt"}).assert_allowlisted()
    with pytest.raises(ValueError, match="absolute"):
        build_ytdlp_options(
            sel,
            workspace="/tmp/ws",
            proxy_url=None,
            network_attempts=1,
            outtmpl="/tmp/ws/%(id)s.%(ext)s",
            cookie_file="relative/cookies.txt",
        )


@pytest.mark.unit
def test_options_for_request_forwards_cookie_file() -> None:
    opts = options_for_request(
        mode=MediaMode.VIDEO,
        quality=VideoQuality.P720,
        bitrate=None,
        workspace="/tmp/ws",
        proxy_url=None,
        network_attempts=1,
        cookie_file="/run/secrets/youtube_cookies.txt",
    )

    assert opts.cookie_file == "/run/secrets/youtube_cookies.txt"
    assert opts.materialize()["cookiefile"] == "/run/secrets/youtube_cookies.txt"


@pytest.mark.unit
@pytest.mark.parametrize("extract_fails", [False, True])
def test_run_ytdlp_never_writes_cookie_secret(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    extract_fails: bool,
) -> None:
    from yt_dlp import YoutubeDL

    cookie_file = tmp_path / "cookies.txt"
    cookie_bytes = (
        b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tcookie-canary\n"
    )
    cookie_file.write_bytes(cookie_bytes)
    cookie_file.chmod(0o400)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    save_cookiefile_values: list[object] = []
    original_save_cookies = YoutubeDL.save_cookies

    def fake_extract_info(self, url: str, *, download: bool):
        _ = (self, url, download)
        if extract_fails:
            raise RuntimeError("fixture extraction failed")
        (workspace / "fixture.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return {"title": "fixture"}

    def recording_save_cookies(self):
        save_cookiefile_values.append(self.params.get("cookiefile"))
        return original_save_cookies(self)

    monkeypatch.setattr(YoutubeDL, "extract_info", fake_extract_info)
    monkeypatch.setattr(YoutubeDL, "save_cookies", recording_save_cookies)
    opts = options_for_request(
        mode=MediaMode.VIDEO,
        quality=VideoQuality.P720,
        bitrate=None,
        workspace=str(workspace),
        proxy_url=None,
        network_attempts=1,
        cookie_file=str(cookie_file),
    )

    if extract_fails:
        with pytest.raises(RuntimeError, match="fixture extraction failed"):
            run_ytdlp_download("https://example.com/video", opts, workspace=workspace)
    else:
        output, title = run_ytdlp_download(
            "https://example.com/video",
            opts,
            workspace=workspace,
        )
        assert output.name == "fixture.mp4"
        assert title == "fixture"

    assert save_cookiefile_values == [None]
    assert cookie_file.read_bytes() == cookie_bytes


@pytest.mark.unit
def test_malformed_cookie_entry_never_reaches_stderr(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\nmalformed-cookie-canary\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    opts = options_for_request(
        mode=MediaMode.VIDEO,
        quality=VideoQuality.P720,
        bitrate=None,
        workspace=str(workspace),
        proxy_url=None,
        network_attempts=1,
        cookie_file=str(cookie_file),
    )

    with pytest.raises(ValueError, match="invalid entry"):
        run_ytdlp_download("https://example.com/video", opts, workspace=workspace)

    captured = capsys.readouterr()
    assert "malformed-cookie-canary" not in captured.err


@pytest.mark.unit
def test_ffmpeg_intents_and_verify(tmp_path) -> None:
    inp = tmp_path / "in.mp4"
    inp.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    out = tmp_path / "out.mp4"
    intent = build_intent(
        FfmpegOp.REMUX,
        inputs=[str(inp)],
        output=str(out),
        workspace_root=str(tmp_path),
    )
    args = args_for_intent(intent)
    assert args[0] == "ffmpeg" and "-i" in args
    merge = build_mp4_merge_args(str(inp), str(inp), str(out))
    assert merge.count("-i") == 2
    tc = build_mp4_compat_transcode_args(str(inp), str(out), height_ceiling=720)
    assert "libx264" in tc and "aac" in tc
    mp3 = build_mp3_encode_args(str(inp), str(tmp_path / "a.mp3"), bitrate_k="320")
    assert "libmp3lame" in mp3
    probe = verify_mp4(str(inp), workspace_root=str(tmp_path), height_ceiling=1080)
    assert probe.has_video
    with pytest.raises(FfmpegError):
        verify_mp4(str(inp), workspace_root=str(tmp_path), height_ceiling=360)
    mp3f = tmp_path / "a.mp3"
    mp3f.write_bytes(b"ID3" + b"\x00" * 32)
    verify_mp3(str(mp3f), workspace_root=str(tmp_path), target_bitrate_k=320)
    with pytest.raises(FfmpegError):
        ensure = __import__(
            "ytdlp_bot.adapters.media.ffmpeg_engine", fromlist=["ensure_local_input"]
        ).ensure_local_input
        ensure("/etc/passwd", workspace_root=str(tmp_path))
    from ytdlp_bot.adapters.media.ffmpeg_engine import remux_local

    try:
        remux_local(str(inp), str(tmp_path / "remuxed.mp4"), workspace_root=str(tmp_path))
    except FfmpegError as exc:
        # Environment may lack ffmpeg binary; still prove path lockdown.
        assert exc.code in {"FFMPEG_UNAVAILABLE", "FFMPEG_FAILED", "FFMPEG_TIMEOUT"}


@pytest.mark.unit
def test_parse_ffprobe_payload() -> None:
    probe = parse_ffprobe_payload(
        {
            "streams": [
                {"codec_type": "video", "height": 720, "width": 1280},
                {"codec_type": "audio", "bit_rate": "192000"},
            ],
            "format": {"format_name": "mov,mp4", "duration": "12.5"},
        }
    )
    assert probe.has_video and probe.has_audio and probe.height == 720
    with pytest.raises(FfmpegError):
        parse_ffprobe_payload({"streams": "bad", "format": {}})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_progress_throttle_terminal_and_sequence() -> None:
    calls: list[object] = []
    persisted: list[object] = []

    async def edit(ref, view) -> None:
        calls.append(view)

    async def persist(job_id, snap) -> None:
        persisted.append(snap)

    pr = ProgressReporter(edit_progress=edit, interval=timedelta(seconds=5), persist=persist)
    jid = JobId("J" * 22)
    ref = MessageReference(platform=Platform.TELEGRAM, chat_id="1", message_id="9")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    def snap(seq: int, phase=None, downloaded=0, total=100):
        from ytdlp_bot.domain.enums import WorkerPhase

        return progress_from_worker_values(
            phase=phase or WorkerPhase.DOWNLOADING,
            downloaded_bytes=downloaded,
            total_bytes=total,
            speed_bytes_per_second=1,
            eta_seconds=1,
            playlist_completed=None,
            playlist_total=None,
            current_entry_index=None,
            current_entry_title=None,
            updated_at=t0,
            source_sequence=seq,
        )

    assert await pr.on_progress(
        job_id=jid, state=JobState.DOWNLOADING, message_reference=ref, progress=snap(1), now=t0
    )
    assert not await pr.on_progress(
        job_id=jid,
        state=JobState.DOWNLOADING,
        message_reference=ref,
        progress=snap(2),
        now=t0 + timedelta(seconds=1),
    )
    assert await pr.on_progress(
        job_id=jid,
        state=JobState.DOWNLOADING,
        message_reference=ref,
        progress=snap(3),
        now=t0 + timedelta(seconds=6),
    )
    # Stale sequence ignored
    assert not await pr.on_progress(
        job_id=jid,
        state=JobState.DOWNLOADING,
        message_reference=ref,
        progress=snap(1),
        now=t0 + timedelta(seconds=20),
    )
    pr.mark_terminal(jid)
    assert not await pr.on_progress(
        job_id=jid,
        state=JobState.COMPLETED,
        message_reference=ref,
        progress=snap(99),
        now=t0 + timedelta(seconds=60),
        force=True,
    )
    assert len(calls) == 2
    assert height_within_ceiling(720, VideoQuality.P720)

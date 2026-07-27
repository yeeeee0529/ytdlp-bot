"""Media worker process entrypoint (NDJSON protocol on stdin/stdout)."""

from __future__ import annotations

import contextlib
import json
import sys
import traceback
from pathlib import Path

from ytdlp_bot.adapters.media.archive import build_artifact_display_name
from ytdlp_bot.adapters.media.ffmpeg_engine import ensure_local_input, probe_media
from ytdlp_bot.adapters.media.worker_protocol import WorkerRequestMessage, parse_ndjson_line
from ytdlp_bot.adapters.media.ytdlp_engine import options_for_request, run_ytdlp_download
from ytdlp_bot.domain.enums import AudioBitrate, MediaMode, VideoQuality, WorkerPhase


def _emit(
    event_type: str, sequence: int, job_id: str, phase: str | None = None, **payload: object
) -> None:
    msg = {
        "type": event_type,
        "sequence": sequence,
        "job_id": job_id,
        "phase": phase,
        "payload": payload,
    }
    sys.stdout.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    """Read one WorkerRequest JSON line from stdin and process media."""
    _ = argv
    try:
        line = sys.stdin.readline()
        if not line:
            return 2
        data = parse_ndjson_line(line)
        req = WorkerRequestMessage.from_dict(data)
    except Exception as exc:
        sys.stderr.write(f"protocol error: {type(exc).__name__}\n")
        return 2

    seq = 0

    def emit(event_type: str, phase: str | None = None, **payload: object) -> None:
        nonlocal seq
        seq += 1
        _emit(event_type, seq, req.job_id, phase=phase, **payload)

    try:
        mode = MediaMode(req.mode)
        quality = VideoQuality(req.video_quality) if req.video_quality else None
        bitrate = AudioBitrate(req.audio_bitrate) if req.audio_bitrate else None
        workspace = Path(req.workspace_path)
        workspace.mkdir(parents=True, exist_ok=True)

        emit("phase_changed", phase=WorkerPhase.INSPECTING.value)
        emit("phase_changed", phase=WorkerPhase.DOWNLOADING.value)

        opts = options_for_request(
            mode=mode,
            quality=quality,
            bitrate=bitrate,
            workspace=str(workspace),
            proxy_url=req.proxy_url,
            network_attempts=req.network_attempts,
            cookie_file=req.cookie_file_path,
        )
        # Fixture mode for deterministic CI: YTDLP_BOT_FIXTURE_WORKER=1
        import os

        if os.environ.get("YTDLP_BOT_FIXTURE_WORKER") == "1":
            if mode is MediaMode.AUDIO:
                out = workspace / "audio.mp3"
                out.write_bytes(b"ID3" + b"\x00" * 64)
                media_type = "audio/mpeg"
                name = "audio.mp3"
            else:
                out = workspace / "video.mp4"
                out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
                media_type = "video/mp4"
                name = "video.mp4"
        else:
            out, media_title = run_ytdlp_download(req.source_url, opts, workspace=workspace)
            emit("phase_changed", phase=WorkerPhase.POST_PROCESSING.value)
            local = ensure_local_input(str(out), workspace_root=str(workspace))
            probe = probe_media(local)
            if mode is MediaMode.VIDEO and not probe.has_video and probe.has_audio:
                emit(
                    "worker_failed",
                    phase=WorkerPhase.POST_PROCESSING.value,
                    error_code="AUDIO_ONLY_SOURCE",
                )
                return 1
            media_type = "audio/mpeg" if mode is MediaMode.AUDIO else "video/mp4"
            ext = out.suffix.lstrip(".") or ("mp3" if mode is MediaMode.AUDIO else "mp4")
            # Prefer original title; fallback to on-disk name (title-based outtmpl).
            name = build_artifact_display_name(media_title, ext) if media_title else out.name

        emit("phase_changed", phase=WorkerPhase.POST_PROCESSING.value)
        emit(
            "artifact_candidate",
            phase=WorkerPhase.FINALIZING.value,
            path=str(out),
            display_name=name,
            media_type=media_type,
            byte_size=out.stat().st_size,
        )
        emit("worker_succeeded", phase=WorkerPhase.FINALIZING.value)
        return 0
    except Exception as exc:
        sys.stderr.write(f"worker failed: {type(exc).__name__}: {exc}\n")
        sys.stderr.write(traceback.format_exc()[-500:])
        with contextlib.suppress(Exception):
            emit(
                "worker_failed",
                phase=WorkerPhase.POST_PROCESSING.value,
                error_code="INTERNAL_ERROR",
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

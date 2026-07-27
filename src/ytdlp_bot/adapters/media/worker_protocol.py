"""NDJSON worker protocol messages (controller ↔ worker)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ytdlp_bot.domain.enums import AudioBitrate, MediaMode, VideoQuality, WorkerPhase
from ytdlp_bot.domain.identity import JobId


@dataclass(frozen=True, slots=True)
class WorkerRequestMessage:
    type: Literal["worker_request"] = "worker_request"
    job_id: str = ""
    source_url: str = ""
    mode: str = "video"
    video_quality: str | None = None
    audio_bitrate: str | None = None
    workspace_path: str = ""
    proxy_url: str | None = None
    network_attempts: int = 3
    correlation_id: str = ""
    playlist_enabled: bool = True
    cookie_file_path: str | None = None

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerRequestMessage:
        cookie_file_path = data.get("cookie_file_path")
        if cookie_file_path is not None and not isinstance(cookie_file_path, str):
            raise ValueError("cookie_file_path must be string or null")
        return cls(
            job_id=str(data["job_id"]),
            source_url=str(data["source_url"]),
            mode=str(data["mode"]),
            video_quality=data.get("video_quality"),
            audio_bitrate=data.get("audio_bitrate"),
            workspace_path=str(data["workspace_path"]),
            proxy_url=data.get("proxy_url"),
            network_attempts=int(data.get("network_attempts", 3)),
            correlation_id=str(data.get("correlation_id", "")),
            playlist_enabled=bool(data.get("playlist_enabled", True)),
            cookie_file_path=cookie_file_path,
        )


@dataclass(frozen=True, slots=True)
class WorkerEventMessage:
    type: str
    sequence: int
    job_id: str
    phase: str | None = None
    payload: dict[str, Any] | None = None

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "type": self.type,
                "sequence": self.sequence,
                "job_id": self.job_id,
                "phase": self.phase,
                "payload": self.payload or {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerEventMessage:
        return cls(
            type=str(data["type"]),
            sequence=int(data["sequence"]),
            job_id=str(data["job_id"]),
            phase=data.get("phase"),
            payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
        )


def parse_ndjson_line(line: str) -> dict[str, Any]:
    data = json.loads(line)
    if not isinstance(data, dict) or "type" not in data:
        raise ValueError("invalid protocol message")
    return data


def request_from_domain(
    job_id: JobId,
    *,
    source_url: str,
    mode: MediaMode,
    quality: VideoQuality | None,
    bitrate: AudioBitrate | None,
    workspace_path: str,
    proxy_url: str | None,
    network_attempts: int,
    correlation_id: str,
    cookie_file_path: str | None = None,
) -> WorkerRequestMessage:
    return WorkerRequestMessage(
        job_id=job_id.value,
        source_url=source_url,
        mode=mode.value,
        video_quality=quality.value if quality else None,
        audio_bitrate=bitrate.value if bitrate else None,
        workspace_path=workspace_path,
        proxy_url=proxy_url,
        network_attempts=network_attempts,
        correlation_id=correlation_id,
        cookie_file_path=cookie_file_path,
    )


def phase_event(job_id: str, sequence: int, phase: WorkerPhase) -> WorkerEventMessage:
    return WorkerEventMessage(
        type="phase_changed",
        sequence=sequence,
        job_id=job_id,
        phase=phase.value,
    )

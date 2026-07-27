"""Media worker supervision ports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ytdlp_bot.domain.enums import AudioBitrate, MediaMode, VideoQuality, WorkerPhase
from ytdlp_bot.domain.identity import JobId


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    """Immutable request sent to a media worker process."""

    job_id: JobId
    source_url: str
    mode: MediaMode
    video_quality: VideoQuality | None
    audio_bitrate: AudioBitrate | None
    workspace_path: str
    proxy_url: str | None
    network_attempts: int
    correlation_id: str
    playlist_enabled: bool = True
    cookie_file_path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    """Typed worker lifecycle/progress event (logical base)."""

    sequence: int
    job_id: JobId
    kind: str
    phase: WorkerPhase | None = None
    payload: dict[str, object] | None = None


class EventSink(Protocol):
    async def emit(self, event: WorkerEvent) -> None: ...


class MediaWorkerSupervisor(Protocol):
    async def start(self, request: WorkerRequest, sink: EventSink) -> None: ...

    async def request_cancel(self, job_id: JobId) -> None: ...

    async def force_terminate(self, job_id: JobId) -> None: ...

    async def active_jobs(self) -> Sequence[JobId]: ...

    async def shutdown(self) -> None: ...

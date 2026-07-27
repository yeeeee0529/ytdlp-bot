"""Worker protocol coverage for operator-managed cookie files."""

from __future__ import annotations

import json

import pytest

from ytdlp_bot.adapters.media.worker_protocol import WorkerRequestMessage


@pytest.mark.unit
def test_worker_request_round_trips_cookie_file_path_without_cookie_contents() -> None:
    message = WorkerRequestMessage(
        job_id="A" * 22,
        source_url="https://www.youtube.com/watch?v=fixture",
        mode="video",
        video_quality="720p",
        workspace_path="/tmp/workspace",
        correlation_id="correlation",
        cookie_file_path="/run/secrets/youtube_cookies.txt",
    )

    encoded = message.to_json_line()
    decoded = WorkerRequestMessage.from_dict(json.loads(encoded))

    assert decoded.cookie_file_path == "/run/secrets/youtube_cookies.txt"
    assert "SID=" not in encoded
    assert "cookie_file_path" in encoded


@pytest.mark.unit
def test_worker_request_without_cookie_path_remains_backward_compatible() -> None:
    decoded = WorkerRequestMessage.from_dict(
        {
            "type": "worker_request",
            "job_id": "A" * 22,
            "source_url": "https://example.com/video",
            "mode": "video",
            "workspace_path": "/tmp/workspace",
        }
    )

    assert decoded.cookie_file_path is None


@pytest.mark.unit
def test_worker_request_rejects_non_string_cookie_path() -> None:
    with pytest.raises(ValueError, match="cookie_file_path"):
        WorkerRequestMessage.from_dict(
            {
                "type": "worker_request",
                "job_id": "A" * 22,
                "source_url": "https://example.com/video",
                "mode": "video",
                "workspace_path": "/tmp/workspace",
                "cookie_file_path": 123,
            }
        )

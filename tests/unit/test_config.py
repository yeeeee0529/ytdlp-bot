"""CFG module unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ytdlp_bot.config import (
    ConfigurationError,
    apply_runtime_overrides,
    canonicalize_public_base_url,
    load_static_config,
    minimal_valid_toml,
    parse_byte_size,
    parse_duration_seconds,
)
from ytdlp_bot.domain.enums import AccessMode, Platform


def _secrets() -> tuple[dict[str, str], object]:
    values = {
        "env:TG_TOKEN": "telegram-token-canary-NOT-FOR-LOGS",
        "env:SIGNING_SECRET": "S" * 32,
        "env:PROXY": "http://proxy.example.invalid:8080",
    }

    def reader(ref: str) -> str:
        if ref not in values:
            raise ConfigurationError(f"unresolved secret ref: {ref}")
        return values[ref]

    return values, reader


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Config dir is separate from data roots (matches deployment layout)."""
    cfg_path = tmp_path / "cfg" / "config.toml"
    art = tmp_path / "data" / "artifacts"
    db = tmp_path / "data" / "state" / "service.sqlite3"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg_path, art, db


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12h", 12 * 3600),
        ("1d", 86400),
        ("30m", 1800),
        ("3600s", 3600),
        ("90", 90),
        (120, 120),
    ],
)
def test_parse_duration_valid(raw: str | int, expected: int) -> None:
    assert parse_duration_seconds(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "0", "-1", "abc", "1x", 0, -5])
def test_parse_duration_invalid(raw: object) -> None:
    with pytest.raises(ConfigurationError):
        parse_duration_seconds(raw)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1024", 1024),
        ("1KiB", 1024),
        ("1MiB", 1024**2),
        ("1GiB", 1024**3),
        (2048, 2048),
    ],
)
def test_parse_byte_size_valid(raw: str | int, expected: int) -> None:
    assert parse_byte_size(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "0", "-1", "1XB", 0])
def test_parse_byte_size_invalid(raw: object) -> None:
    with pytest.raises(ConfigurationError):
        parse_byte_size(raw)  # type: ignore[arg-type]


@pytest.mark.unit
def test_load_minimal_valid(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    cfg = load_static_config(
        text,
        config_path=cfg_path,
        secret_reader=reader,  # type: ignore[arg-type]
        check_writable=False,
    )
    assert cfg.app.worker_concurrency == 2
    assert cfg.platforms[Platform.TELEGRAM].enabled is True
    assert cfg.artifacts.public_base_url == "https://downloads.example.invalid"
    assert cfg.media.cookie_file is None
    assert cfg.startup_summary()["cookie_file_configured"] is False
    assert "telegram-token-canary" not in str(cfg.startup_summary())
    assert "S" * 32 not in str(cfg.startup_summary())


@pytest.mark.unit
def test_cookie_file_ref_is_file_only_and_redacted_from_summary(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    cookie_file = tmp_path / "secrets" / "youtube-cookie-canary.txt"
    cookie_file.parent.mkdir()
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-canary\n",
        encoding="utf-8",
    )
    cookie_file.chmod(0o600)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    text = text.replace(
        'default_audio_bitrate = "320k"',
        (f'default_audio_bitrate = "320k"\ncookie_file_ref = "file:{cookie_file}"'),
    )

    cfg = load_static_config(
        text,
        config_path=cfg_path,
        secret_reader=reader,  # type: ignore[arg-type]
        check_writable=False,
    )

    assert cfg.media.cookie_file == cookie_file.resolve()
    summary = str(cfg.startup_summary())
    assert "'cookie_file_configured': True" in summary
    assert cookie_file.name not in summary
    assert "secret-canary" not in summary


@pytest.mark.unit
@pytest.mark.parametrize(
    "cookie_ref",
    [
        "env:YOUTUBE_COOKIES",
        "relative/cookies.txt",
        "file:relative/cookies.txt",
    ],
)
def test_cookie_file_ref_rejects_non_file_or_relative_refs(
    tmp_path: Path,
    cookie_ref: str,
) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    text = text.replace(
        'default_audio_bitrate = "320k"',
        f'default_audio_bitrate = "320k"\ncookie_file_ref = "{cookie_ref}"',
    )

    with pytest.raises(ConfigurationError, match=r"file secret reference|absolute"):
        load_static_config(
            text,
            config_path=cfg_path,
            secret_reader=reader,  # type: ignore[arg-type]
            check_writable=False,
        )


@pytest.mark.unit
def test_cookie_file_ref_rejects_world_writable_file(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookie_file.chmod(0o666)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    text = text.replace(
        'default_audio_bitrate = "320k"',
        (f'default_audio_bitrate = "320k"\ncookie_file_ref = "file:{cookie_file}"'),
    )

    with pytest.raises(ConfigurationError, match="world-writable"):
        load_static_config(
            text,
            config_path=cfg_path,
            secret_reader=reader,  # type: ignore[arg-type]
            check_writable=False,
        )


@pytest.mark.unit
def test_cookie_file_ref_validation_does_not_read_secret_contents(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_bytes(b"\xff\xfe\xfd")
    cookie_file.chmod(0o600)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    text = text.replace(
        'default_audio_bitrate = "320k"',
        (f'default_audio_bitrate = "320k"\ncookie_file_ref = "file:{cookie_file}"'),
    )

    cfg = load_static_config(
        text,
        config_path=cfg_path,
        secret_reader=reader,  # type: ignore[arg-type]
        check_writable=False,
    )

    assert cfg.media.cookie_file == cookie_file


@pytest.mark.unit
def test_unknown_key_rejected(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    text = text.replace("worker_concurrency = 2", "worker_concurrency = 2\nunknown_field = 1")
    with pytest.raises(ConfigurationError, match="unknown keys"):
        load_static_config(
            text,
            config_path=cfg_path,
            secret_reader=reader,  # type: ignore[arg-type]
            check_writable=False,
        )


@pytest.mark.unit
def test_no_platform_enabled(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    text = text.replace("enabled = true", "enabled = false", 1)
    with pytest.raises(ConfigurationError, match="at least one platform"):
        load_static_config(
            text,
            config_path=cfg_path,
            secret_reader=reader,  # type: ignore[arg-type]
            check_writable=False,
        )


@pytest.mark.unit
def test_public_url_rules() -> None:
    assert canonicalize_public_base_url("https://dl.example.invalid/") == (
        "https://dl.example.invalid"
    )
    with pytest.raises(ConfigurationError):
        canonicalize_public_base_url("http://insecure.example")
    with pytest.raises(ConfigurationError):
        canonicalize_public_base_url("https://x.example?q=1")
    with pytest.raises(ConfigurationError):
        canonicalize_public_base_url("https://user:pass@x.example")


@pytest.mark.unit
def test_path_inside_config_dir_rejected(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path = tmp_path / "cfg" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    art = tmp_path / "cfg" / "artifacts"
    db = tmp_path / "cfg" / "db.sqlite3"
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    with pytest.raises(ConfigurationError, match="configuration directory"):
        load_static_config(
            text,
            config_path=cfg_path,
            secret_reader=reader,  # type: ignore[arg-type]
            check_writable=False,
        )


@pytest.mark.unit
def test_signing_secret_too_short(tmp_path: Path) -> None:
    values, _ = _secrets()
    values["env:SIGNING_SECRET"] = "short"

    def reader(ref: str) -> str:
        return values[ref]

    cfg_path, art, db = _layout(tmp_path)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    with pytest.raises(ConfigurationError, match="32 bytes"):
        load_static_config(
            text,
            config_path=cfg_path,
            secret_reader=reader,
            check_writable=False,
        )


@pytest.mark.unit
def test_admin_requires_enabled_platform(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    text = text.replace(
        'administrators = ["telegram:123456789"]',
        'administrators = ["discord:123456789012345678"]',
    )
    with pytest.raises(ConfigurationError, match="not enabled"):
        load_static_config(
            text,
            config_path=cfg_path,
            secret_reader=reader,  # type: ignore[arg-type]
            check_writable=False,
        )


@pytest.mark.unit
def test_runtime_overrides(tmp_path: Path) -> None:
    _, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    base = load_static_config(
        text,
        config_path=cfg_path,
        secret_reader=reader,  # type: ignore[arg-type]
        check_writable=False,
    )
    updated = apply_runtime_overrides(
        base,
        {
            "worker_concurrency": 4,
            "retention_seconds": 7200,
            "access_mode": "whitelist",
            "capacity_bytes": 200 * 1024 * 1024,
        },
    )
    assert updated.app.worker_concurrency == 4
    assert updated.artifacts.retention_seconds == 7200
    assert updated.access.mode is AccessMode.WHITELIST
    assert updated.storage.capacity_bytes == 200 * 1024 * 1024
    assert base.app.worker_concurrency == 2  # immutability of base


@pytest.mark.unit
def test_startup_summary_redacts_secrets(tmp_path: Path) -> None:
    values, reader = _secrets()
    cfg_path, art, db = _layout(tmp_path)
    text = minimal_valid_toml(artifact_root=str(art), database_path=str(db))
    cfg = load_static_config(
        text,
        config_path=cfg_path,
        secret_reader=reader,  # type: ignore[arg-type]
        check_writable=False,
    )
    blob = str(cfg.startup_summary())
    for canary in values.values():
        assert canary not in blob
    # Exception messages must not leak secrets either.
    try:
        raise ConfigurationError("token resolution failed")
    except ConfigurationError as exc:
        assert values["env:TG_TOKEN"] not in str(exc)

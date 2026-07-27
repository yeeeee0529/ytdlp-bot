"""Static configuration loading, secret resolution, and EffectiveConfig.

Other modules receive typed EffectiveConfig and must not read environment
variables or secret files directly.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ytdlp_bot.domain.enums import AccessMode, AudioBitrate, Platform, VideoQuality
from ytdlp_bot.domain.identity import Identity
from ytdlp_bot.domain.settings import SETTINGS_CATALOG, SettingEffect, validate_setting_value

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigurationError(Exception):
    """Fail-closed configuration validation error (English diagnostic only)."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        self.field = field
        # Never include secret values in the message.
        super().__init__(message)


# ---------------------------------------------------------------------------
# Scalar parsers (CFG-01)
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_BYTE_SIZE_RE = re.compile(r"^(\d+)([KMGTP]?i?B)?$", re.IGNORECASE)
_MAX_DURATION = 10 * 365 * 24 * 3600
_MAX_BYTES = 10 * 1024**4  # 10 TiB logical upper bound for config scalars


def parse_duration_seconds(value: str | int) -> int:
    """Parse duration into seconds with overflow protection."""
    if isinstance(value, int):
        if value <= 0 or value > _MAX_DURATION:
            raise ConfigurationError("duration out of bounds", field="duration")
        return value
    if not isinstance(value, str):
        raise ConfigurationError("duration must be string or int", field="duration")
    text = value.strip().lower()
    if text.isdigit():
        seconds = int(text)
        if seconds <= 0 or seconds > _MAX_DURATION:
            raise ConfigurationError("duration out of bounds", field="duration")
        return seconds
    match = _DURATION_RE.fullmatch(text)
    if not match or text == "":
        raise ConfigurationError("invalid duration syntax", field="duration")
    days, hours, minutes, secs = (int(g or 0) for g in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + secs
    if total <= 0 or total > _MAX_DURATION:
        raise ConfigurationError("duration out of bounds", field="duration")
    return total


def parse_byte_size(value: str | int) -> int:
    """Parse byte size with optional KiB/MiB/GiB suffixes."""
    if isinstance(value, int):
        if value <= 0 or value > _MAX_BYTES:
            raise ConfigurationError("byte size out of bounds", field="bytes")
        return value
    if not isinstance(value, str):
        raise ConfigurationError("byte size must be string or int", field="bytes")
    text = value.strip().replace(" ", "")
    match = _BYTE_SIZE_RE.fullmatch(text)
    if not match:
        raise ConfigurationError("invalid byte size syntax", field="bytes")
    amount = int(match.group(1))
    unit = (match.group(2) or "B").lower()
    multipliers = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
        "tb": 1000**4,
        "tib": 1024**4,
        "pb": 1000**5,
        "pib": 1024**5,
    }
    if unit not in multipliers:
        raise ConfigurationError("invalid byte unit", field="bytes")
    try:
        result = amount * multipliers[unit]
    except OverflowError as exc:
        raise ConfigurationError("byte size overflow", field="bytes") from exc
    if result <= 0 or result > _MAX_BYTES:
        raise ConfigurationError("byte size out of bounds", field="bytes")
    return result


# ---------------------------------------------------------------------------
# Typed configuration sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppConfig:
    default_locale: str = "zh-TW"
    worker_concurrency: int = 2
    progress_interval_seconds: int = 5
    cancellation_grace_seconds: int = 10
    cleanup_interval_seconds: int = 300
    graceful_shutdown_seconds: int = 30
    recent_jobs_limit: int = 10
    terminal_tombstone_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    enabled: bool
    token: str  # resolved secret value (never logged)
    upload_limit_bytes: int


@dataclass(frozen=True, slots=True)
class StorageConfig:
    artifact_root: Path
    database_path: Path
    capacity_bytes: int
    safety_headroom_bytes: int
    unknown_size_initial_reservation_bytes: int
    reservation_growth_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactsConfig:
    retention_seconds: int
    link_expiry_seconds: int
    public_base_url: str
    signing_secret: str  # resolved (never logged)


@dataclass(frozen=True, slots=True)
class HttpConfig:
    bind_host: str
    bind_port: int
    max_concurrent_streams: int
    stream_chunk_bytes: int
    stream_idle_timeout_seconds: int
    trusted_proxy_networks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    outbound_proxy: str | None
    allowed_destination_ports: tuple[int, ...]
    max_redirects: int
    dns_timeout_seconds: int
    request_timeout_seconds: int
    blocked_cidrs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MediaConfig:
    video_qualities: tuple[VideoQuality, ...]
    audio_bitrates: tuple[AudioBitrate, ...]
    default_video_quality: VideoQuality
    default_audio_bitrate: AudioBitrate
    network_attempts: int
    worker_heartbeat_seconds: int
    worker_stall_timeout_seconds: int
    cookie_file: Path | None = None


@dataclass(frozen=True, slots=True)
class AccessConfig:
    mode: AccessMode
    administrators: frozenset[Identity]
    whitelist: frozenset[Identity]


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str
    format: str
    recommended_retention_days: int


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    """Immutable configuration snapshot for all application modules."""

    app: AppConfig
    platforms: Mapping[Platform, PlatformConfig]
    storage: StorageConfig
    artifacts: ArtifactsConfig
    http: HttpConfig
    network: NetworkConfig
    media: MediaConfig
    access: AccessConfig
    logging: LoggingConfig
    config_path: Path
    runtime_overrides: Mapping[str, object] = field(default_factory=dict)

    def startup_summary(self) -> dict[str, object]:
        """Safe, redacted startup summary for logs."""
        enabled = [p.value for p, cfg in self.platforms.items() if cfg.enabled]
        return {
            "default_locale": self.app.default_locale,
            "worker_concurrency": self.app.worker_concurrency,
            "progress_interval_seconds": self.app.progress_interval_seconds,
            "enabled_platforms": enabled,
            "artifact_root": _disclose_path(self.storage.artifact_root),
            "database_path": _disclose_path(self.storage.database_path),
            "capacity_bytes": self.storage.capacity_bytes,
            "safety_headroom_bytes": self.storage.safety_headroom_bytes,
            "retention_seconds": self.artifacts.retention_seconds,
            "link_expiry_seconds": self.artifacts.link_expiry_seconds,
            "public_base_url_host": urlparse(self.artifacts.public_base_url).hostname,
            "http_bind": f"{self.http.bind_host}:{self.http.bind_port}",
            "access_mode": self.access.mode.value,
            "administrator_count": len(self.access.administrators),
            "whitelist_count": len(self.access.whitelist),
            "logging_level": self.logging.level,
            "runtime_override_keys": sorted(self.runtime_overrides.keys()),
            "signing_secret_configured": bool(self.artifacts.signing_secret),
            "outbound_proxy_configured": bool(self.network.outbound_proxy),
            "cookie_file_configured": self.media.cookie_file is not None,
        }


def _disclose_path(path: Path) -> str:
    """Disclose path shape without embedding secrets."""
    return str(path)


# ---------------------------------------------------------------------------
# Secret reader
# ---------------------------------------------------------------------------

SecretReader = Callable[[str], str]


def default_secret_reader(ref: str) -> str:
    """Resolve env:NAME or file:/path secret references."""
    if ref.startswith("env:"):
        name = ref[4:]
        if not name:
            raise ConfigurationError("empty environment secret name")
        value = os.environ.get(name)
        if value is None or value == "":
            raise ConfigurationError(f"unresolved environment secret: {name}")
        return value
    if ref.startswith("file:"):
        path = Path(ref[5:])
        if not path.is_file():
            raise ConfigurationError(f"secret file not found: {path.name}")
        # Reject world-writable secret files when permissions are available.
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o002:
                raise ConfigurationError(f"secret file is world-writable: {path.name}")
        except OSError as exc:
            raise ConfigurationError(f"cannot stat secret file: {path.name}") from exc
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ConfigurationError(f"empty secret file: {path.name}")
        return text
    raise ConfigurationError("unsupported secret reference scheme")


def resolve_secret_file_path(ref: str, *, field_name: str) -> Path:
    """Validate a file-backed secret reference without reading its contents."""
    if not ref.startswith("file:"):
        raise ConfigurationError(
            f"{field_name} must use a file secret reference",
            field=field_name,
        )
    raw_path = ref[5:]
    if not raw_path:
        raise ConfigurationError(f"{field_name} path is empty", field=field_name)
    path = Path(raw_path)
    if not path.is_absolute():
        raise ConfigurationError(f"{field_name} path must be absolute", field=field_name)
    if not path.is_file():
        raise ConfigurationError(f"secret file not found: {path.name}", field=field_name)
    try:
        stat = path.stat()
    except OSError as exc:
        raise ConfigurationError(
            f"cannot stat secret file: {path.name}",
            field=field_name,
        ) from exc
    if stat.st_size <= 0:
        raise ConfigurationError(f"empty secret file: {path.name}", field=field_name)
    if stat.st_mode & 0o002:
        raise ConfigurationError(
            f"secret file is world-writable: {path.name}",
            field=field_name,
        )
    if not os.access(path, os.R_OK):
        raise ConfigurationError(
            f"secret file is not readable: {path.name}",
            field=field_name,
        )
    return path


# ---------------------------------------------------------------------------
# Known TOML keys (strict)
# ---------------------------------------------------------------------------

_KNOWN_SECTIONS = frozenset(
    {
        "app",
        "platforms",
        "storage",
        "artifacts",
        "http",
        "network",
        "media",
        "access",
        "logging",
    }
)

_KNOWN_APP = frozenset(
    {
        "default_locale",
        "worker_concurrency",
        "progress_interval_seconds",
        "cancellation_grace_seconds",
        "cleanup_interval_seconds",
        "graceful_shutdown_seconds",
        "recent_jobs_limit",
        "terminal_tombstone_seconds",
    }
)


def _require_mapping(data: object, name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigurationError(f"section {name} must be a table", field=name)
    return data


def _get_int(section: Mapping[str, Any], key: str, default: int, *, lo: int, hi: int) -> int:
    if key not in section:
        value = default
    else:
        raw = section[key]
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ConfigurationError(f"{key} must be integer", field=key)
        value = raw
    if value < lo or value > hi:
        raise ConfigurationError(f"{key} out of bounds [{lo}, {hi}]", field=key)
    return value


def _reject_unknown(section: Mapping[str, Any], known: frozenset[str], name: str) -> None:
    unknown = set(section) - known
    if unknown:
        raise ConfigurationError(
            f"unknown keys in {name}: {', '.join(sorted(unknown))}",
            field=name,
        )


def canonicalize_public_base_url(url: str) -> str:
    """Require HTTPS, no query/fragment/userinfo, no trailing slash."""
    if not isinstance(url, str) or not url:
        raise ConfigurationError("public_base_url is required", field="public_base_url")
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        raise ConfigurationError("public_base_url must use https", field="public_base_url")
    if parsed.username or parsed.password:
        raise ConfigurationError(
            "public_base_url must not include userinfo", field="public_base_url"
        )
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "public_base_url must not include query or fragment",
            field="public_base_url",
        )
    if not parsed.hostname:
        raise ConfigurationError("public_base_url host is required", field="public_base_url")
    path = parsed.path or ""
    if path == "/":
        path = ""
    if path.endswith("/"):
        path = path.rstrip("/")
    # Rebuild without trailing slash.
    netloc = parsed.hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    canonical = f"https://{netloc}{path}"
    if canonical.endswith("/"):
        raise ConfigurationError("public_base_url must not end with slash", field="public_base_url")
    return canonical


def normalize_path(value: str | Path, *, field_name: str) -> Path:
    path = Path(value).expanduser().resolve()
    if path == Path("/") or str(path) == "/":
        raise ConfigurationError(f"{field_name} must not be filesystem root", field=field_name)
    return path


def validate_path_separation(
    artifact_root: Path,
    database_path: Path,
    config_path: Path,
) -> None:
    """Reject conflicting or unsafe path relationships."""
    if artifact_root == database_path:
        raise ConfigurationError("artifact_root and database_path must differ")
    if database_path == artifact_root or artifact_root in database_path.parents:
        raise ConfigurationError("database_path must not be inside artifact_root")
    config_dir = config_path.parent.resolve()
    if artifact_root == config_dir:
        raise ConfigurationError("artifact_root must not be the configuration directory")
    if config_dir in artifact_root.parents or artifact_root == config_dir:
        raise ConfigurationError("artifact_root must not be inside configuration directory")
    if config_dir in database_path.parents or database_path.parent == config_dir:
        raise ConfigurationError("database_path must not be inside configuration directory")


def _validate_signing_secret(secret: str) -> None:
    # At least 256 bits of operator-generated entropy ≈ 32 bytes.
    if len(secret.encode("utf-8")) < 32:
        raise ConfigurationError("signing secret must be at least 32 bytes", field="signing_secret")


def load_static_config(
    toml_text: str,
    *,
    config_path: Path,
    secret_reader: SecretReader = default_secret_reader,
    check_writable: bool = False,
) -> EffectiveConfig:
    """Parse and validate static TOML configuration (no runtime overrides)."""
    try:
        data = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"invalid TOML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigurationError("root must be a table")
    _reject_unknown(data, _KNOWN_SECTIONS, "root")

    app_raw = _require_mapping(data.get("app", {}), "app")
    _reject_unknown(app_raw, _KNOWN_APP, "app")
    app = AppConfig(
        default_locale=str(app_raw.get("default_locale", "zh-TW")),
        worker_concurrency=_get_int(app_raw, "worker_concurrency", 2, lo=1, hi=16),
        progress_interval_seconds=_get_int(app_raw, "progress_interval_seconds", 5, lo=2, hi=60),
        cancellation_grace_seconds=_get_int(app_raw, "cancellation_grace_seconds", 10, lo=1, hi=60),
        cleanup_interval_seconds=_get_int(app_raw, "cleanup_interval_seconds", 300, lo=60, hi=3600),
        graceful_shutdown_seconds=_get_int(app_raw, "graceful_shutdown_seconds", 30, lo=10, hi=300),
        recent_jobs_limit=_get_int(app_raw, "recent_jobs_limit", 10, lo=1, hi=50),
        terminal_tombstone_seconds=_get_int(
            app_raw, "terminal_tombstone_seconds", 3600, lo=60, hi=86400
        ),
    )
    if app.default_locale != "zh-TW":
        raise ConfigurationError("only zh-TW locale is supported in this release")

    platforms_raw = _require_mapping(data.get("platforms", {}), "platforms")
    platforms: dict[Platform, PlatformConfig] = {}
    for name in ("telegram", "discord"):
        if name not in platforms_raw:
            continue
        section = _require_mapping(platforms_raw[name], f"platforms.{name}")
        known = frozenset(
            {
                "enabled",
                "token_ref",
                "direct_upload_limit_bytes",
                "fallback_upload_limit_bytes",
            }
        )
        _reject_unknown(section, known, f"platforms.{name}")
        enabled = bool(section.get("enabled", False))
        token = ""
        if enabled:
            token_ref = section.get("token_ref")
            if not isinstance(token_ref, str) or not token_ref:
                raise ConfigurationError(
                    f"enabled platform {name} requires token_ref",
                    field=f"platforms.{name}.token_ref",
                )
            token = secret_reader(token_ref)
            if not token:
                raise ConfigurationError(f"empty token for platform {name}")
        limit_key = (
            "direct_upload_limit_bytes" if name == "telegram" else "fallback_upload_limit_bytes"
        )
        limit_default = 50_000_000 if name == "telegram" else 10_485_760
        limit = section.get(limit_key, limit_default)
        if isinstance(limit, str):
            limit = parse_byte_size(limit)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ConfigurationError(f"invalid upload limit for {name}")
        platforms[Platform(name)] = PlatformConfig(
            enabled=enabled,
            token=token,
            upload_limit_bytes=limit,
        )

    if not any(p.enabled for p in platforms.values()):
        raise ConfigurationError("at least one platform must be enabled")

    storage_raw = _require_mapping(data.get("storage", {}), "storage")
    _reject_unknown(
        storage_raw,
        frozenset(
            {
                "artifact_root",
                "database_path",
                "capacity_bytes",
                "safety_headroom_bytes",
                "unknown_size_initial_reservation_bytes",
                "reservation_growth_bytes",
            }
        ),
        "storage",
    )
    if "artifact_root" not in storage_raw or "database_path" not in storage_raw:
        raise ConfigurationError("storage.artifact_root and storage.database_path are required")
    if "capacity_bytes" not in storage_raw:
        raise ConfigurationError("storage.capacity_bytes is required")
    capacity = storage_raw["capacity_bytes"]
    if isinstance(capacity, str):
        capacity = parse_byte_size(capacity)
    if not isinstance(capacity, int) or isinstance(capacity, bool):
        raise ConfigurationError("capacity_bytes must be integer")
    if capacity < 100 * 1024 * 1024 or capacity > _MAX_BYTES:
        raise ConfigurationError("capacity_bytes out of bounds")

    artifact_root = normalize_path(str(storage_raw["artifact_root"]), field_name="artifact_root")
    database_path = normalize_path(str(storage_raw["database_path"]), field_name="database_path")
    validate_path_separation(artifact_root, database_path, config_path)

    def _bytes_field(key: str, default: int) -> int:
        raw = storage_raw.get(key, default)
        if isinstance(raw, str):
            return parse_byte_size(raw)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise ConfigurationError(f"{key} invalid")
        return raw

    storage = StorageConfig(
        artifact_root=artifact_root,
        database_path=database_path,
        capacity_bytes=capacity,
        safety_headroom_bytes=_bytes_field("safety_headroom_bytes", 1024**3),
        unknown_size_initial_reservation_bytes=_bytes_field(
            "unknown_size_initial_reservation_bytes", 256 * 1024**2
        ),
        reservation_growth_bytes=_bytes_field("reservation_growth_bytes", 256 * 1024**2),
    )

    if check_writable:
        for path in (storage.artifact_root, storage.database_path.parent):
            path.mkdir(parents=True, exist_ok=True)
            if not os.access(path, os.W_OK):
                raise ConfigurationError(f"path not writable: {path.name}")

    artifacts_raw = _require_mapping(data.get("artifacts", {}), "artifacts")
    _reject_unknown(
        artifacts_raw,
        frozenset(
            {
                "retention_seconds",
                "link_expiry_seconds",
                "public_base_url",
                "signing_secret_ref",
            }
        ),
        "artifacts",
    )
    retention = artifacts_raw.get("retention_seconds", 43200)
    if isinstance(retention, str):
        retention = parse_duration_seconds(retention)
    link_exp = artifacts_raw.get("link_expiry_seconds", 3600)
    if isinstance(link_exp, str):
        link_exp = parse_duration_seconds(link_exp)
    if not isinstance(retention, int) or retention < 60:
        raise ConfigurationError("retention_seconds invalid")
    if not isinstance(link_exp, int) or link_exp < 60:
        raise ConfigurationError("link_expiry_seconds invalid")
    public_url = canonicalize_public_base_url(str(artifacts_raw.get("public_base_url", "")))
    signing_ref = artifacts_raw.get("signing_secret_ref")
    if not isinstance(signing_ref, str) or not signing_ref:
        raise ConfigurationError("signing_secret_ref is required")
    signing_secret = secret_reader(signing_ref)
    _validate_signing_secret(signing_secret)
    artifacts = ArtifactsConfig(
        retention_seconds=retention,
        link_expiry_seconds=link_exp,
        public_base_url=public_url,
        signing_secret=signing_secret,
    )

    http_raw = _require_mapping(data.get("http", {}), "http")
    trusted = http_raw.get("trusted_proxy_networks", [])
    if not isinstance(trusted, list) or not all(isinstance(x, str) for x in trusted):
        raise ConfigurationError("trusted_proxy_networks must be string array")
    http = HttpConfig(
        bind_host=str(http_raw.get("bind_host", "0.0.0.0")),
        bind_port=_get_int(http_raw, "bind_port", 8080, lo=1, hi=65535),
        max_concurrent_streams=_get_int(http_raw, "max_concurrent_streams", 8, lo=1, hi=256),
        stream_chunk_bytes=_get_int(
            http_raw, "stream_chunk_bytes", 1048576, lo=4096, hi=16 * 1024**2
        ),
        stream_idle_timeout_seconds=_get_int(
            http_raw, "stream_idle_timeout_seconds", 60, lo=5, hi=600
        ),
        trusted_proxy_networks=tuple(trusted),
    )

    network_raw = _require_mapping(data.get("network", {}), "network")
    ports_raw = network_raw.get("allowed_destination_ports", [80, 443])
    if not isinstance(ports_raw, list) or not all(isinstance(p, int) for p in ports_raw):
        raise ConfigurationError("allowed_destination_ports must be int array")
    blocked = network_raw.get("blocked_cidrs", [])
    if not isinstance(blocked, list) or not all(isinstance(x, str) for x in blocked):
        raise ConfigurationError("blocked_cidrs must be string array")
    proxy: str | None = None
    proxy_ref = network_raw.get("outbound_proxy_ref")
    if proxy_ref is not None:
        if not isinstance(proxy_ref, str):
            raise ConfigurationError("outbound_proxy_ref must be string")
        proxy = secret_reader(proxy_ref)
    network = NetworkConfig(
        outbound_proxy=proxy,
        allowed_destination_ports=tuple(int(p) for p in ports_raw),
        max_redirects=_get_int(network_raw, "max_redirects", 10, lo=0, hi=20),
        dns_timeout_seconds=_get_int(network_raw, "dns_timeout_seconds", 5, lo=1, hi=60),
        request_timeout_seconds=_get_int(network_raw, "request_timeout_seconds", 30, lo=1, hi=300),
        blocked_cidrs=tuple(blocked),
    )

    media_raw = _require_mapping(data.get("media", {}), "media")
    cookie_file: Path | None = None
    cookie_file_ref = media_raw.get("cookie_file_ref")
    if cookie_file_ref is not None:
        if not isinstance(cookie_file_ref, str):
            raise ConfigurationError(
                "cookie_file_ref must be string",
                field="cookie_file_ref",
            )
        cookie_file = resolve_secret_file_path(
            cookie_file_ref,
            field_name="cookie_file_ref",
        )
    vq = media_raw.get(
        "video_qualities",
        ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p"],
    )
    ab = media_raw.get("audio_bitrates", ["128k", "192k", "256k", "320k"])
    try:
        video_qualities = tuple(VideoQuality(x) for x in vq)
        audio_bitrates = tuple(AudioBitrate(x) for x in ab)
        default_vq = VideoQuality(str(media_raw.get("default_video_quality", "best")))
        default_ab = AudioBitrate(str(media_raw.get("default_audio_bitrate", "320k")))
    except ValueError as exc:
        raise ConfigurationError("invalid media quality/bitrate enum") from exc
    media = MediaConfig(
        video_qualities=video_qualities,
        audio_bitrates=audio_bitrates,
        default_video_quality=default_vq,
        default_audio_bitrate=default_ab,
        network_attempts=_get_int(media_raw, "network_attempts", 3, lo=1, hi=10),
        worker_heartbeat_seconds=_get_int(media_raw, "worker_heartbeat_seconds", 15, lo=5, hi=120),
        worker_stall_timeout_seconds=_get_int(
            media_raw, "worker_stall_timeout_seconds", 90, lo=30, hi=600
        ),
        cookie_file=cookie_file,
    )

    access_raw = _require_mapping(data.get("access", {}), "access")
    mode_raw = str(access_raw.get("initial_mode", "allow_all"))
    try:
        mode = AccessMode(mode_raw)
    except ValueError as exc:
        raise ConfigurationError("invalid access initial_mode") from exc
    admins_raw = access_raw.get("administrators", [])
    whitelist_raw = access_raw.get("initial_whitelist", [])
    if not isinstance(admins_raw, list) or not isinstance(whitelist_raw, list):
        raise ConfigurationError("administrators and initial_whitelist must be arrays")
    administrators: set[Identity] = set()
    for item in admins_raw:
        if not isinstance(item, str):
            raise ConfigurationError("administrator entries must be strings")
        try:
            identity = Identity.parse(item)
        except Exception as exc:
            raise ConfigurationError(f"invalid administrator identity: {item}") from exc
        if identity.platform not in platforms or not platforms[identity.platform].enabled:
            raise ConfigurationError(
                f"administrator platform not enabled: {identity.platform.value}"
            )
        administrators.add(identity)
    whitelist: set[Identity] = set()
    for item in whitelist_raw:
        if not isinstance(item, str):
            raise ConfigurationError("whitelist entries must be strings")
        whitelist.add(Identity.parse(item))
    access = AccessConfig(
        mode=mode,
        administrators=frozenset(administrators),
        whitelist=frozenset(whitelist),
    )

    logging_raw = _require_mapping(data.get("logging", {}), "logging")
    logging_cfg = LoggingConfig(
        level=str(logging_raw.get("level", "INFO")).upper(),
        format=str(logging_raw.get("format", "json")),
        recommended_retention_days=_get_int(
            logging_raw, "recommended_retention_days", 14, lo=1, hi=365
        ),
    )
    if logging_cfg.format not in {"json", "text"}:
        raise ConfigurationError("logging.format must be json or text")

    return EffectiveConfig(
        app=app,
        platforms=platforms,
        storage=storage,
        artifacts=artifacts,
        http=http,
        network=network,
        media=media,
        access=access,
        logging=logging_cfg,
        config_path=config_path.resolve(),
    )


def apply_runtime_overrides(
    base: EffectiveConfig,
    overrides: Mapping[str, object],
) -> EffectiveConfig:
    """Overlay validated mutable runtime settings onto an immutable snapshot."""
    cleaned: dict[str, object] = {}
    app = base.app
    artifacts = base.artifacts
    storage = base.storage
    access = base.access

    for key, value in overrides.items():
        if key not in SETTINGS_CATALOG:
            # Ignore stale unknown keys rather than failing the whole process.
            continue
        validated = validate_setting_value(key, value)
        cleaned[key] = validated
        definition = SETTINGS_CATALOG[key]
        # Effect timing is recorded in catalog; values still update the snapshot
        # fields used when creating new artifacts/links or for immediate modes.
        if key == "worker_concurrency":
            app = replace(app, worker_concurrency=int(validated))
        elif key == "retention_seconds":
            # NEW_ARTIFACTS effect — snapshot holds the value for new artifacts.
            artifacts = replace(artifacts, retention_seconds=int(validated))
            _ = definition.effect  # documented
        elif key == "link_expiry_seconds":
            artifacts = replace(artifacts, link_expiry_seconds=int(validated))
            assert definition.effect is SettingEffect.NEW_LINKS
        elif key == "capacity_bytes":
            storage = replace(storage, capacity_bytes=int(validated))
        elif key == "access_mode":
            access = replace(access, mode=AccessMode(str(validated)))

    return replace(
        base,
        app=app,
        artifacts=artifacts,
        storage=storage,
        access=access,
        runtime_overrides=cleaned,
    )


def load_config_from_path(
    path: Path,
    *,
    secret_reader: SecretReader = default_secret_reader,
    runtime_overrides: Mapping[str, object] | None = None,
    check_writable: bool = True,
) -> EffectiveConfig:
    """Primary configuration entry point used by bootstrap."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigurationError(f"configuration file not found: {config_path.name}")
    text = config_path.read_text(encoding="utf-8")
    base = load_static_config(
        text,
        config_path=config_path,
        secret_reader=secret_reader,
        check_writable=check_writable,
    )
    if runtime_overrides:
        return apply_runtime_overrides(base, runtime_overrides)
    return base


def minimal_valid_toml(
    *,
    artifact_root: str,
    database_path: str,
    telegram_token_ref: str = "env:TG_TOKEN",
    signing_secret_ref: str = "env:SIGNING_SECRET",
    public_base_url: str = "https://downloads.example.invalid",
) -> str:
    """Helper for tests: minimal valid configuration document."""
    return f"""
[app]
default_locale = "zh-TW"
worker_concurrency = 2

[platforms.telegram]
enabled = true
token_ref = "{telegram_token_ref}"
direct_upload_limit_bytes = 50000000

[platforms.discord]
enabled = false

[storage]
artifact_root = "{artifact_root}"
database_path = "{database_path}"
capacity_bytes = 1073741824

[artifacts]
retention_seconds = 43200
link_expiry_seconds = 3600
public_base_url = "{public_base_url}"
signing_secret_ref = "{signing_secret_ref}"

[http]
bind_host = "127.0.0.1"
bind_port = 8080

[network]
allowed_destination_ports = [80, 443]

[media]
default_video_quality = "best"
default_audio_bitrate = "320k"

[access]
initial_mode = "allow_all"
administrators = ["telegram:123456789"]

[logging]
level = "INFO"
format = "json"
"""

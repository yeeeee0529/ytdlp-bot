"""Dependency injection and full application composition."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from ytdlp_bot.adapters.http.application import create_app
from ytdlp_bot.adapters.http.downloads import DownloadService
from ytdlp_bot.adapters.http.health import HealthController, ReadinessState
from ytdlp_bot.adapters.media.worker_supervisor import ProcessWorkerSupervisor
from ytdlp_bot.adapters.network.preflight import AiohttpPreflightClient, EgressSelfTest
from ytdlp_bot.adapters.network.resolver import SystemDnsResolver
from ytdlp_bot.adapters.persistence.sqlite.connection import InstanceLock, open_connection
from ytdlp_bot.adapters.persistence.sqlite.migrate import apply_migrations
from ytdlp_bot.adapters.persistence.sqlite.repositories import (
    SqliteAccessDenialRepository,
    SqliteAccessRepository,
    SqliteAdminConfirmationRepository,
    SqliteArtifactRepository,
    SqliteJobPayloadRepository,
    SqliteJobRepository,
    SqliteSettingsRepository,
)
from ytdlp_bot.adapters.platform.discord import DiscordPlatformAdapter
from ytdlp_bot.adapters.platform.telegram import TelegramPlatformAdapter
from ytdlp_bot.adapters.security.logging_config import configure_logging
from ytdlp_bot.adapters.security.metrics import InMemoryMetricsSink
from ytdlp_bot.adapters.security.signed_tokens import DownloadLinkIssuer, HmacTokenSigner
from ytdlp_bot.adapters.storage.capacity import CapacityManager
from ytdlp_bot.adapters.storage.local_store import LocalArtifactStore
from ytdlp_bot.application.admin_service import AdminService
from ytdlp_bot.application.artifact_access import (
    ArtifactAccessCoordinator,
    InMemoryArtifactLeaseRegistry,
)
from ytdlp_bot.application.authorization import AuthorizationService
from ytdlp_bot.application.capacity_eviction import CapacityEvictionService
from ytdlp_bot.application.cleanup import CleanupService
from ytdlp_bot.application.command_service import CommandService
from ytdlp_bot.application.delivery import DeliveryService
from ytdlp_bot.application.dispatcher import ClockAwareDispatcher
from ytdlp_bot.application.job_service import JobService
from ytdlp_bot.application.orchestrator import Orchestrator
from ytdlp_bot.application.progress_reporter import ProgressReporter
from ytdlp_bot.application.url_safety import UrlSafetyService
from ytdlp_bot.config import EffectiveConfig, load_config_from_path
from ytdlp_bot.domain.enums import (
    AudioBitrate,
    JobState,
    MediaMode,
    Platform,
    VideoQuality,
)
from ytdlp_bot.domain.errors import DomainError
from ytdlp_bot.domain.jobs import Job
from ytdlp_bot.domain.progress import FinalOutcomeView
from ytdlp_bot.ports.media import WorkerEvent, WorkerRequest

log = logging.getLogger("ytdlp_bot.bootstrap")


class RandomIdGenerator:
    """Production ID generator using secrets."""

    def __init__(self) -> None:
        import base64
        import secrets

        self._secrets = secrets
        self._b64 = base64

    def _token(self, n: int = 16) -> str:
        return self._b64.urlsafe_b64encode(self._secrets.token_bytes(n)).decode("ascii").rstrip("=")

    def job_id(self) -> str:
        return self._token()

    def artifact_id(self) -> str:
        return self._token()

    def confirmation_id(self) -> str:
        return self._token()

    def link_nonce(self) -> str:
        return self._token()

    def storage_key(self) -> str:
        return self._token(18)

    def correlation_id(self) -> str:
        return self._token()


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        import time

        return time.monotonic()


@dataclass
class AppRuntime:
    config: EffectiveConfig
    readiness: ReadinessState
    health: HealthController
    metrics: InMemoryMetricsSink
    lock: InstanceLock | None
    conn: object | None
    store: LocalArtifactStore | None
    http_app: web.Application | None
    command_service: CommandService | None = None
    dispatcher_task: asyncio.Task[None] | None = None
    cleanup_task: asyncio.Task[None] | None = None
    platform_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    worker_supervisor: ProcessWorkerSupervisor | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


async def bootstrap(
    config_path: Path,
    *,
    check_writable: bool = True,
    acquire_lock: bool = True,
    fixture_workers: bool = False,
    start_background: bool = True,
) -> AppRuntime:
    """Composition root: config → lock → DB → services → HTTP → platforms."""
    readiness = ReadinessState()
    metrics = InMemoryMetricsSink()
    configure_logging(level="INFO")

    config = load_config_from_path(config_path, check_writable=check_writable)
    # Apply configured log level (including DEBUG).
    configure_logging(level=config.logging.level)
    log.info(
        "configuration loaded",
        extra={
            "event": "startup.config",
            "component": "bootstrap",
        },
    )
    readiness.configuration = True

    lock = None
    if acquire_lock:
        lock_path = config.storage.database_path.parent / "instance.lock"
        lock = InstanceLock(lock_path)
        lock.acquire()

    conn = await open_connection(config.storage.database_path)
    readiness.database = True
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    await apply_migrations(conn, now_ms=now_ms)
    readiness.migrations = True

    store = LocalArtifactStore(config.storage.artifact_root)
    # Storage self-test
    from ytdlp_bot.domain.identity import JobId as JID

    probe_job = JID("P" * 22)
    try:
        if (store.workspaces / probe_job.value).exists():
            await store.delete_workspace(probe_job)
        ws = await store.create_job_workspace(probe_job)
        probe = Path(ws) / "probe.bin"
        probe.write_bytes(b"ok")
        key = "K" * 22
        await store.atomically_publish(str(probe), key)
        await store.delete(key)
        await store.delete_workspace(probe_job)
        readiness.storage = True
    except Exception as exc:
        log.error("storage self-test failed: %s", type(exc).__name__)
        readiness.storage = False

    jobs = SqliteJobRepository(conn)
    arts = SqliteArtifactRepository(conn)
    payloads = SqliteJobPayloadRepository(conn)
    settings = SqliteSettingsRepository(
        conn,
        {
            "capacity_bytes": config.storage.capacity_bytes,
            "retention_seconds": config.artifacts.retention_seconds,
            "link_expiry_seconds": config.artifacts.link_expiry_seconds,
            "access_mode": config.access.mode.value,
            "worker_concurrency": config.app.worker_concurrency,
        },
    )
    access_repo = SqliteAccessRepository(conn, config.access.mode)
    denial_repo = SqliteAccessDenialRepository(conn)
    confirmations = SqliteAdminConfirmationRepository(conn)

    dns = SystemDnsResolver(timeout_seconds=config.network.dns_timeout_seconds)
    preflight = AiohttpPreflightClient(
        dns=dns,
        proxy_url=config.network.outbound_proxy,
        max_redirects=config.network.max_redirects,
        timeout_seconds=float(config.network.request_timeout_seconds),
        allowed_ports=frozenset(config.network.allowed_destination_ports),
    )
    egress = EgressSelfTest(dns=dns, preflight=preflight, proxy_url=config.network.outbound_proxy)
    egress_results = await egress.run()
    readiness.egress = bool(egress_results.get("ok"))
    metrics.gauge("egress_ready", 1.0 if readiness.egress else 0.0)

    clock = WallClock()
    ids = RandomIdGenerator()
    auth = AuthorizationService(
        access=access_repo,
        jobs=jobs,
        artifacts=arts,
        administrators=config.access.administrators,
        denials=denial_repo,
    )
    url_safety = UrlSafetyService(
        dns=dns,
        preflight=preflight,
        allowed_ports=frozenset(config.network.allowed_destination_ports),
        max_redirects=config.network.max_redirects,
    )

    # Platform adapters (tokens from config)
    tg_cfg = config.platforms.get(Platform.TELEGRAM)
    dc_cfg = config.platforms.get(Platform.DISCORD)
    telegram = TelegramPlatformAdapter(
        upload_limit_bytes=tg_cfg.upload_limit_bytes if tg_cfg else 50_000_000,
        bot_token=tg_cfg.token if tg_cfg and tg_cfg.enabled else "",
    )
    discord = DiscordPlatformAdapter(
        upload_limit_bytes=dc_cfg.upload_limit_bytes if dc_cfg else 10_485_760,
        bot_token=dc_cfg.token if dc_cfg and dc_cfg.enabled else "",
    )
    # Prefer telegram as default platform port for ack when enabled.
    platform_port = telegram if (tg_cfg and tg_cfg.enabled) else discord

    job_service = JobService(
        auth=auth,
        url_safety=url_safety,
        jobs=jobs,
        payloads=payloads,
        platform=platform_port,
        clock=clock,
        ids=ids,
        recent_limit=config.app.recent_jobs_limit,
    )

    admin = AdminService(
        auth=auth,
        settings=settings,
        access=access_repo,
        confirmations=confirmations,
        id_confirmation=ids,
        jobs=jobs,
        artifacts=arts,
        files=store,
    )
    command_service = CommandService(jobs=job_service, admin=admin, admission_open=True)

    telegram.command_handler = command_service.handle
    discord.command_handler = command_service.handle
    telegram.access_probe = command_service.probe_inbound_message
    discord.access_probe = command_service.probe_inbound_message

    signer = HmacTokenSigner(
        config.artifacts.signing_secret.encode("utf-8"),
        public_base_url=config.artifacts.public_base_url,
    )
    capacity = CapacityManager(
        store=store,
        capacity_bytes=config.storage.capacity_bytes,
        safety_headroom_bytes=config.storage.safety_headroom_bytes,
        unknown_size_initial_reservation_bytes=config.storage.unknown_size_initial_reservation_bytes,
        reservation_growth_bytes=config.storage.reservation_growth_bytes,
    )
    leases = InMemoryArtifactLeaseRegistry()
    capacity_eviction = CapacityEvictionService(
        capacity=capacity,
        artifacts=arts,
        files=store,
        jobs=jobs,
        leases=leases,
    )
    delivery = DeliveryService(
        platform=platform_port,
        link_issuer=DownloadLinkIssuer(signer),
        link_lifetime_seconds=config.artifacts.link_expiry_seconds,
        path_resolver=store,
    )
    progress = ProgressReporter(
        edit_progress=platform_port.edit_progress,
        interval=__import__("datetime").timedelta(seconds=config.app.progress_interval_seconds),
    )
    from ytdlp_bot.application.capacity_publish import PublishService

    publisher = PublishService(
        capacity=capacity,
        store=store,
        artifacts=arts,
        jobs=jobs,
        payloads=payloads,
        ids=ids,
        retention_seconds=config.artifacts.retention_seconds,
        eviction=capacity_eviction,
    )
    orchestrator = Orchestrator(
        jobs=jobs,
        artifacts=arts,
        payloads=payloads,
        store=store,
        delivery=delivery,
        progress=progress,
        ids=ids,
        retention_seconds=config.artifacts.retention_seconds,
        now_fn=clock.now,
        publisher=publisher,
    )
    workers = ProcessWorkerSupervisor(fixture_mode=fixture_workers)

    async def launch(job: Job) -> None:
        payload = await payloads.get(job.job_id)
        if payload is None:
            log.warning(
                "launch skipped: missing payload",
                extra={"event": "job.launch_skip", "job_id": job.job_id.value},
            )
            return
        log.info(
            "launching job",
            extra={
                "event": "job.launch",
                "job_id": job.job_id.value,
                "state": job.state.value,
                "source_display": job.source_display,
            },
        )
        # Reserve capacity before spawning work; reclaim oldest artifacts if needed.
        now = clock.now()
        await capacity_eviction.ensure_capacity(
            needed_bytes=capacity.unknown_size_initial,
            now=now,
        )
        try:
            await capacity.reserve(
                job.job_id,
                known_size=None,
                now=now,
            )
        except DomainError as exc:
            log.warning(
                "launch capacity denied",
                extra={
                    "event": "job.launch_capacity_denied",
                    "job_id": job.job_id.value,
                    "error_code": exc.code.value,
                },
            )
            await payloads.delete(job.job_id)
            current = await jobs.get(job.job_id) or job
            await jobs.transition(
                job.job_id,
                expected_version=current.version,
                new_state=JobState.FAILED,
                error_code=exc.code,
            )
            if current.message_reference is not None:
                await platform_port.send_final(
                    current.message_reference,
                    FinalOutcomeView(
                        job_id=job.job_id,
                        outcome="failed",
                        message_key="outcome.failed",
                        error_code=exc.code,
                    ),
                )
            return
        ws = await store.create_job_workspace(job.job_id)
        quality = None
        bitrate = None
        if job.request_mode is MediaMode.VIDEO:
            try:
                quality = VideoQuality(job.selected_preset)
            except ValueError:
                quality = VideoQuality.BEST
        else:
            try:
                bitrate = AudioBitrate(job.selected_preset)
            except ValueError:
                bitrate = AudioBitrate.K320

        class Sink:
            async def emit(self, event: WorkerEvent) -> None:
                await orchestrator.handle_event(event)

        await workers.start(
            WorkerRequest(
                job_id=job.job_id,
                source_url=payload.source_url,
                mode=job.request_mode,
                video_quality=quality,
                audio_bitrate=bitrate,
                workspace_path=ws,
                proxy_url=config.network.outbound_proxy,
                network_attempts=config.media.network_attempts,
                correlation_id=ids.correlation_id(),
                cookie_file_path=(
                    str(config.media.cookie_file) if config.media.cookie_file is not None else None
                ),
            ),
            Sink(),
        )

    async def claim_next(*, controller_id: str, now: datetime, expected_states: list[JobState]):
        return await jobs.claim_next(
            controller_id=controller_id, now=now, expected_states=expected_states
        )

    dispatcher = ClockAwareDispatcher(
        claim_next=claim_next,
        launch=launch,
        controller_id="controller-1",
        concurrency=config.app.worker_concurrency,
        now_fn=clock.now,
    )

    cleanup = CleanupService(
        jobs=jobs,
        artifacts=arts,
        files=store,
        payloads=payloads,
        capacity=capacity_eviction,
    )

    access = ArtifactAccessCoordinator(arts, leases)
    downloads = DownloadService(
        artifacts=arts,
        store=store,
        signer=signer,
        access=access,
        clock=clock.now,
        stream_chunk_bytes=config.http.stream_chunk_bytes,
        max_concurrent_streams=config.http.max_concurrent_streams,
    )
    health = HealthController(readiness=readiness)
    app = create_app(downloads=downloads, health=health, expose_health=True)

    readiness.recovery = True
    readiness.dispatcher = True
    readiness.platforms = any(p.enabled for p in config.platforms.values())
    readiness.http = True
    command_service.admission_open = readiness.is_ready()

    runtime = AppRuntime(
        config=config,
        readiness=readiness,
        health=health,
        metrics=metrics,
        lock=lock,
        conn=conn,
        store=store,
        http_app=app,
        command_service=command_service,
        worker_supervisor=workers,
    )

    if start_background:
        runtime.dispatcher_task = asyncio.create_task(dispatcher.run_forever())

        async def cleanup_loop() -> None:
            while not runtime.stop_event.is_set():
                try:
                    await cleanup.expire_due_artifacts(now=clock.now(), limit=20)
                except Exception as exc:
                    log.debug("cleanup tick failed: %s", type(exc).__name__)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        runtime.stop_event.wait(), timeout=config.app.cleanup_interval_seconds
                    )

        runtime.cleanup_task = asyncio.create_task(cleanup_loop())

        if tg_cfg and tg_cfg.enabled and telegram.bot_token:
            runtime.platform_tasks.append(asyncio.create_task(telegram.start_polling()))
        if dc_cfg and dc_cfg.enabled and discord.bot_token:
            runtime.platform_tasks.append(asyncio.create_task(discord.start_gateway()))

    # Capacity is retained via publisher/orchestrator/launch closures.
    _ = capacity
    return runtime


async def shutdown_runtime(runtime: AppRuntime) -> None:
    runtime.readiness.admission_open = False
    if runtime.command_service is not None:
        runtime.command_service.admission_open = False
    runtime.stop_event.set()
    if runtime.worker_supervisor is not None:
        await runtime.worker_supervisor.shutdown()
    for task in runtime.platform_tasks:
        task.cancel()
    if runtime.dispatcher_task is not None:
        runtime.dispatcher_task.cancel()
    if runtime.cleanup_task is not None:
        runtime.cleanup_task.cancel()
    if runtime.conn is not None:
        await runtime.conn.close()  # type: ignore[union-attr]
    if runtime.lock is not None:
        runtime.lock.release()

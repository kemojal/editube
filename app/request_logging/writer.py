from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, text

from app.db.log_models import ApiPayloadLog, ApiRequestLog

from .config import RequestLogSettings
from .database import write_engine


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestLogRecord:
    request: dict[str, Any]
    payload: dict[str, Any] | None = None


class RequestLogWriter:
    def __init__(self, settings: RequestLogSettings):
        self.settings = settings
        self.queue: queue.Queue[RequestLogRecord] = queue.Queue(settings.queue_max_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.enqueued = 0
        self.written = 0
        self.dropped = 0
        self.payloads_shed = 0
        self.failed = 0
        self.last_error: str | None = None

    def start(self) -> None:
        if not self.settings.enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="editube-request-log-writer",
                daemon=True,
            )
            self._thread.start()

    def enqueue(self, record: RequestLogRecord) -> bool:
        self.start()
        # Ciphertext dominates queue memory. Preserve metadata during a slow
        # database/outage by shedding payloads before the queue becomes full.
        if record.payload is not None and self.queue.qsize() >= max(
            1, int(self.settings.queue_max_size * 0.8)
        ):
            metadata = dict(record.request)
            metadata.update(
                {
                    "payload_present": False,
                    "capture_reason": "queue_pressure_metadata_only",
                    "request_body_state": "dropped_queue_pressure",
                    "response_body_state": "dropped_queue_pressure",
                }
            )
            record = RequestLogRecord(request=metadata, payload=None)
            self.payloads_shed += 1
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.error("Request-log queue full; dropped=%s", self.dropped)
            return False
        self.enqueued += 1
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.enabled,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "queue_depth": self.queue.qsize(),
            "queue_capacity": self.settings.queue_max_size,
            "enqueued": self.enqueued,
            "written": self.written,
            "dropped": self.dropped,
            "payloads_shed": self.payloads_shed,
            "failed": self.failed,
            "last_error": self.last_error,
        }

    def _run(self) -> None:
        interval = self.settings.flush_interval_ms / 1000
        while not self._stop.is_set() or not self.queue.empty():
            batch: list[RequestLogRecord] = []
            try:
                batch.append(self.queue.get(timeout=interval))
            except queue.Empty:
                continue
            deadline = time.monotonic() + interval
            while len(batch) < self.settings.batch_size:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout <= 0:
                    break
                try:
                    batch.append(self.queue.get(timeout=timeout))
                except queue.Empty:
                    break
            try:
                self._write_with_retry(batch)
            finally:
                for _ in batch:
                    self.queue.task_done()

    def _write_with_retry(self, batch: list[RequestLogRecord]) -> None:
        for attempt in range(3):
            try:
                self._write_batch(batch)
                self.written += len(batch)
                self.last_error = None
                return
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                if attempt < 2:
                    time.sleep(0.2 * (2**attempt))
        self.failed += len(batch)
        logger.error(
            "Request-log batch permanently failed; records=%s total_failed=%s error=%s",
            len(batch),
            self.failed,
            self.last_error,
        )

    def _write_batch(self, batch: list[RequestLogRecord]) -> None:
        requests = [record.request for record in batch]
        payloads = [record.payload for record in batch if record.payload is not None]
        with write_engine(self.settings).begin() as connection:
            connection.execute(insert(ApiRequestLog.__table__), requests)
            if payloads:
                connection.execute(insert(ApiPayloadLog.__table__), payloads)

    def maintain(self) -> dict[str, int]:
        """Run the migration-owned retention/rollup function via the writer role."""
        with write_engine(self.settings).begin() as connection:
            row = connection.execute(
                text("SELECT * FROM log.maintain_request_logs()"),
            ).mappings().one()
        return {key: int(value) for key, value in row.items()}


_global_lock = threading.Lock()
_global_writer: RequestLogWriter | None = None


def get_global_writer(settings: RequestLogSettings | None = None) -> RequestLogWriter:
    global _global_writer
    settings = settings or RequestLogSettings.from_env()
    with _global_lock:
        if _global_writer is None or _global_writer.settings != settings:
            if _global_writer is not None:
                _global_writer.stop()
            _global_writer = RequestLogWriter(settings)
        return _global_writer


def start_global_writer(settings: RequestLogSettings | None = None) -> RequestLogWriter:
    writer = get_global_writer(settings)
    writer.start()
    return writer


def stop_global_writer() -> None:
    global _global_writer
    with _global_lock:
        writer = _global_writer
        _global_writer = None
    if writer:
        writer.stop()

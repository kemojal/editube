"""Worker → API realtime bridge over Redis pub/sub.

RQ jobs run in a separate process and cannot reach the API's in-process
WebSocket manager, so job status has historically been polled (transcription
every 3s, pipeline every 4s, …). Jobs publish small user-addressed events to
one Redis channel; the API process runs a single subscriber task that forwards
each event to that user's live notification sockets. Clients treat these as
cache-invalidation hints and keep their polling as a fallback, so everything
here is best-effort: a lost event costs one poll interval, never correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

CHANNEL = "editube:user-events"

_sync_client: Any = None


def publish_user_event(user_id: int | None, event: dict[str, Any]) -> None:
    """Publish one event addressed to a user. Sync, worker-safe, never raises."""
    global _sync_client
    if not user_id:
        return
    url = os.environ.get("REDIS_URL")
    if not url:
        return
    try:
        if _sync_client is None:
            import redis

            _sync_client = redis.Redis.from_url(
                url, socket_connect_timeout=2, socket_timeout=2
            )
        _sync_client.publish(
            CHANNEL, json.dumps({"user_id": int(user_id), "event": event})
        )
    except Exception:
        logger.warning("Realtime publish failed (event dropped)", exc_info=True)
        _sync_client = None


def publish_video_update(
    user_id: int | None, video_id: int, kind: str, status: str, **extra: Any
) -> None:
    """Convenience shape shared by transcription/proxy/export publishers."""
    publish_user_event(
        user_id,
        {
            "event": "video.update",
            "payload": {"video_id": video_id, "kind": kind, "status": status, **extra},
        },
    )


async def run_subscriber() -> None:
    """Forward published events to live sockets. Reconnects forever; cancel to stop."""
    from app.websocket_manager import notifications_ws_manager

    url = os.environ.get("REDIS_URL")
    if not url:
        logger.info("REDIS_URL not set; realtime bridge disabled")
        return
    import redis.asyncio as aioredis

    while True:
        client = None
        try:
            client = aioredis.from_url(url)
            pubsub = client.pubsub()
            await pubsub.subscribe(CHANNEL)
            logger.info("Realtime bridge subscribed to %s", CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    await notifications_ws_manager.send_to_user(
                        int(payload["user_id"]), payload["event"]
                    )
                except Exception:
                    logger.debug("Dropped malformed realtime event", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Realtime subscriber lost connection; retrying", exc_info=True)
            await asyncio.sleep(5)
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass

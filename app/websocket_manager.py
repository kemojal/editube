from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class NotificationConnectionManager:
    def __init__(self) -> None:
        self._connections_by_user: dict[int, set[WebSocket]] = defaultdict(set)

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections_by_user[user_id].add(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        sockets = self._connections_by_user.get(user_id)
        if not sockets:
            return
        sockets.discard(websocket)
        if not sockets:
            self._connections_by_user.pop(user_id, None)

    async def send_to_user(self, user_id: int, event: dict[str, Any]) -> None:
        sockets = list(self._connections_by_user.get(user_id, set()))
        for websocket in sockets:
            try:
                await websocket.send_json(event)
            except Exception:
                self.disconnect(user_id, websocket)


notifications_ws_manager = NotificationConnectionManager()

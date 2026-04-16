from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
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


class ReviewRoomConnectionManager:
    def __init__(self) -> None:
        self._room_connections: dict[str, dict[int, set[WebSocket]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self._presence: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        self._room_seq: dict[str, int] = defaultdict(int)
        self._room_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._room_controls: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"locked": False, "host_session_id": None, "muted_sessions": set()}
        )

    async def connect(self, room_id: str, session_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self._room_connections[room_id][session_id].add(websocket)

    def disconnect(self, room_id: str, session_id: int, websocket: WebSocket) -> None:
        room = self._room_connections.get(room_id)
        if not room:
            return
        sockets = room.get(session_id)
        if sockets:
            sockets.discard(websocket)
            if not sockets:
                room.pop(session_id, None)
        if not room:
            self._room_connections.pop(room_id, None)
            self._presence.pop(room_id, None)

    async def broadcast(self, room_id: str, event: dict[str, Any]) -> None:
        room = self._room_connections.get(room_id, {})
        for session_id, sockets in list(room.items()):
            for websocket in list(sockets):
                try:
                    await websocket.send_json(event)
                except Exception:
                    self.disconnect(room_id, session_id, websocket)

    async def close_session(self, room_id: str, session_id: int, code: int = 1000) -> None:
        room = self._room_connections.get(room_id, {})
        for websocket in list(room.get(session_id, set())):
            try:
                await websocket.close(code=code)
            except Exception:
                self.disconnect(room_id, session_id, websocket)

    def upsert_presence(
        self, room_id: str, session_id: int, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        now_iso = datetime.now(timezone.utc).isoformat()
        prev = self._presence[room_id].get(session_id, {})
        payload["last_seen_at"] = now_iso
        payload["status"] = prev.get("status", "active")
        self._presence[room_id][session_id] = payload
        return self.list_presence(room_id)

    def heartbeat(self, room_id: str, session_id: int) -> list[dict[str, Any]]:
        row = self._presence.get(room_id, {}).get(session_id)
        if not row:
            return self.list_presence(room_id)
        row["last_seen_at"] = datetime.now(timezone.utc).isoformat()
        row["status"] = "active"
        self._presence[room_id][session_id] = row
        return self.list_presence(room_id)

    def remove_presence(self, room_id: str, session_id: int) -> list[dict[str, Any]]:
        self._presence.get(room_id, {}).pop(session_id, None)
        return self.list_presence(room_id)

    def list_presence(self, room_id: str) -> list[dict[str, Any]]:
        rows = list(self._presence.get(room_id, {}).values())
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for row in rows:
            ts_raw = row.get("last_seen_at")
            status = "active"
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    delta_s = (now - ts).total_seconds()
                    if delta_s > 60:
                        status = "away"
                    elif delta_s > 20:
                        status = "idle"
                except Exception:
                    status = row.get("status", "active")
            next_row = {**row, "status": status}
            out.append(next_row)
        return out

    def next_seq(self, room_id: str) -> int:
        self._room_seq[room_id] += 1
        return self._room_seq[room_id]

    def add_event(self, room_id: str, event: dict[str, Any]) -> dict[str, Any]:
        seq = self.next_seq(room_id)
        envelope = {**event, "seq": seq}
        self._room_events[room_id].append(envelope)
        if len(self._room_events[room_id]) > 500:
            self._room_events[room_id] = self._room_events[room_id][-500:]
        return envelope

    def replay_since(self, room_id: str, last_seq: int) -> list[dict[str, Any]]:
        return [e for e in self._room_events.get(room_id, []) if int(e.get("seq", 0)) > last_seq]

    def set_lock(self, room_id: str, locked: bool, host_session_id: int | None) -> dict[str, Any]:
        state = self._room_controls[room_id]
        state["locked"] = bool(locked)
        state["host_session_id"] = host_session_id
        return {
            "locked": state["locked"],
            "host_session_id": state["host_session_id"],
            "muted_sessions": sorted(list(state["muted_sessions"])),
        }

    def mute_session(self, room_id: str, target_session_id: int, muted: bool) -> dict[str, Any]:
        state = self._room_controls[room_id]
        muted_set = state["muted_sessions"]
        if muted:
            muted_set.add(target_session_id)
        else:
            muted_set.discard(target_session_id)
        return {
            "muted_sessions": sorted(list(muted_set)),
            "locked": bool(state["locked"]),
            "host_session_id": state.get("host_session_id"),
        }

    def is_muted(self, room_id: str, session_id: int) -> bool:
        return session_id in self._room_controls[room_id]["muted_sessions"]

    def controls_state(self, room_id: str) -> dict[str, Any]:
        state = self._room_controls[room_id]
        return {
            "locked": bool(state["locked"]),
            "host_session_id": state.get("host_session_id"),
            "muted_sessions": sorted(list(state["muted_sessions"])),
        }


review_room_ws_manager = ReviewRoomConnectionManager()

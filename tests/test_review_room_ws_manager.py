from app.websocket_manager import ReviewRoomConnectionManager


def test_presence_upsert_and_remove() -> None:
    manager = ReviewRoomConnectionManager()
    room = "review:1"
    snapshot = manager.upsert_presence(
        room,
        10,
        {"session_id": 10, "guest_name": "A", "playhead": 12},
    )
    assert len(snapshot) == 1
    assert snapshot[0]["session_id"] == 10

    snapshot = manager.upsert_presence(
        room,
        11,
        {"session_id": 11, "guest_name": "B", "playhead": 22},
    )
    assert len(snapshot) == 2

    snapshot = manager.remove_presence(room, 10)
    assert len(snapshot) == 1
    assert snapshot[0]["session_id"] == 11


def test_event_replay_and_sequence() -> None:
    manager = ReviewRoomConnectionManager()
    room = "review:2"
    ev1 = manager.add_event(room, {"event": "presence.snapshot", "payload": []})
    ev2 = manager.add_event(room, {"event": "chat.message", "payload": {"body": "hello"}})
    assert ev1["seq"] == 1
    assert ev2["seq"] == 2
    replay = manager.replay_since(room, 1)
    assert len(replay) == 1
    assert replay[0]["event"] == "chat.message"


def test_controls_lock_and_mute_state() -> None:
    manager = ReviewRoomConnectionManager()
    room = "review:3"
    state = manager.set_lock(room, True, 12)
    assert state["locked"] is True
    assert state["host_session_id"] == 12
    muted = manager.mute_session(room, target_session_id=99, muted=True)
    assert 99 in muted["muted_sessions"]
    assert manager.is_muted(room, 99) is True
    muted = manager.mute_session(room, target_session_id=99, muted=False)
    assert 99 not in muted["muted_sessions"]


def test_presence_heartbeat_keeps_active() -> None:
    manager = ReviewRoomConnectionManager()
    room = "review:4"
    manager.upsert_presence(room, 21, {"session_id": 21, "guest_name": "Ping"})
    snapshot = manager.heartbeat(room, 21)
    assert len(snapshot) == 1
    assert snapshot[0]["status"] == "active"

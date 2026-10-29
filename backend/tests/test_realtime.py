"""WebSocket-уведомления: fan-out записи уведомления в открытые сокеты пользователя."""
from __future__ import annotations

import uuid

import pytest

from app.services import notifications, realtime

pytestmark = pytest.mark.asyncio


class DummyWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def test_push_delivers_to_open_sockets(seeded, db_session):
    uid = seeded["teacher1@example.com"]
    ws1, ws2 = DummyWS(), DummyWS()
    realtime.hub.connect(uid, ws1)
    realtime.hub.connect(uid, ws2)
    try:
        assert realtime.hub.online(uid)
        await notifications.push(
            db_session,
            user_id=uuid.UUID(uid),
            type_=notifications.TASK_ASSIGNED,
            title="Вам поставили новую задачу",
            body="Ведомость за октябрь",
            entity_type="task",
            entity_id=uuid.uuid4(),
        )
        await db_session.commit()
        for ws in (ws1, ws2):
            assert len(ws.sent) == 1
            msg = ws.sent[0]
            assert msg["type"] == "notification"
            assert msg["title"] == "Вам поставили новую задачу"
            assert msg["entity_type"] == "task"
    finally:
        realtime.hub.disconnect(uid, ws1)
        realtime.hub.disconnect(uid, ws2)
    assert not realtime.hub.online(uid)


async def test_send_to_offline_user_is_noop(seeded):
    sent = await realtime.notify_user(seeded["teacher2@example.com"], {"type": "notification", "title": "x"})
    assert sent == 0

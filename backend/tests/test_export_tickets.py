"""Одноразовые билеты: выгрузка данных (ПЭП) и подключение WebSocket.

Закрывает два замечания review/REVIEW.md:
  * §P1-4 — дамп БД и архив всех файлов школы скачивались по обычному access-токену,
    без подписи «код + слово». Теперь нужен билет из POST /system/export-ticket;
  * §5.6  — WebSocket принимал access-токен в query-строке, а она попадает
    в access-логи nginx/DSM. Токен заменён одноразовым короткоживущим билетом.

Проверяются и контракт HTTP, и сам сервис билетов (одноразовость, срок жизни,
привязка к цели и к пользователю, ограничение размера хранилища).
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi import WebSocketDisconnect

from tests.conftest import auth, export_ticket

# asyncio_mode = auto (pytest.ini): метка не нужна, а синхронные unit-тесты
# сервиса билетов с ней дают предупреждение.


# --------------------------------------------------------------- сервис билетов
def test_ticket_is_single_use():
    from app.services import tickets

    t = tickets.issue("user-1", tickets.PURPOSE_DB_BACKUP)
    assert t.token and t.purpose == tickets.PURPOSE_DB_BACKUP
    assert 0 < t.expires_in <= 600

    tickets.consume(t.token, tickets.PURPOSE_DB_BACKUP, "user-1")   # первый раз — успешно
    with pytest.raises(Exception) as exc:
        tickets.consume(t.token, tickets.PURPOSE_DB_BACKUP, "user-1")
    assert exc.value.code == "TICKET_INVALID"


def test_ticket_is_bound_to_purpose_and_user():
    from app.errors import AppError
    from app.services import tickets

    t = tickets.issue("user-1", tickets.PURPOSE_FILES_ZIP)

    with pytest.raises(AppError) as exc:
        tickets.consume(t.token, tickets.PURPOSE_DB_BACKUP, "user-1")
    assert exc.value.code == "TICKET_INVALID"

    t2 = tickets.issue("user-1", tickets.PURPOSE_DB_BACKUP)
    with pytest.raises(AppError) as exc:
        tickets.consume(t2.token, tickets.PURPOSE_DB_BACKUP, "user-2")
    assert exc.value.code == "TICKET_INVALID"


def test_ticket_missing_and_expired_have_distinct_codes():
    from app.errors import AppError
    from app.services import tickets

    with pytest.raises(AppError) as exc:
        tickets.consume(None, tickets.PURPOSE_DB_BACKUP, "user-1")
    assert exc.value.code == "TICKET_MISSING"

    t = tickets.issue("user-1", tickets.PURPOSE_DB_BACKUP)
    # «состариваем» билет напрямую: sleep в тестах — лишние секунды прогона
    user_id, purpose, _ = tickets._STORE[t.token]
    tickets._STORE[t.token] = (user_id, purpose, time.monotonic() - 1)
    with pytest.raises(AppError) as exc:
        tickets.consume(t.token, tickets.PURPOSE_DB_BACKUP, "user-1")
    assert exc.value.code == "TICKET_EXPIRED"
    assert tickets.peek(t.token, tickets.PURPOSE_DB_BACKUP) is None


def test_unknown_purpose_rejected():
    from app.services import tickets

    with pytest.raises(Exception):
        tickets.issue("user-1", "system:что-то-ещё")
    with pytest.raises(Exception):
        tickets.consume("x", "system:что-то-ещё", "user-1")


def test_store_does_not_grow_without_limit():
    """Билеты создаёт аутентифицированный пользователь, но рост хранилища
    от внешнего ввода ограничен: протухшие убираются, сверх лимита — сбрасываются."""
    from app.services import tickets

    before = tickets.stats()["max"]
    alive = [tickets.issue(f"u{i}", tickets.PURPOSE_WEBSOCKET) for i in range(50)]
    stats = tickets.stats()
    assert stats["max"] == before
    assert stats["active"] >= len(alive)
    # билеты с истёкшим сроком вычищаются служебным проходом
    for t in alive:
        user_id, purpose, _ = tickets._STORE[t.token]
        tickets._STORE[t.token] = (user_id, purpose, time.monotonic() - 1)
    tickets._prune_expired()
    assert all(t.token not in tickets._STORE for t in alive)


# ------------------------------------------------------- HTTP: подпись выгрузки
async def test_export_ticket_requires_owner_and_pep(client, seeded):
    teacher = await auth(client, "teacher1@example.com")
    resp = await teacher.post("/system/export-ticket",
                              json={"target": "db_backup", "pep_code": "000000", "pep_word": "голубой-кит"})
    assert resp.status_code == 403

    owner = await auth(client, "owner@example.com")
    # подпись не приложена — операция не выполняется (409 с подсказкой, без фолбэка на пароль)
    resp = await owner.post("/system/export-ticket", json={"target": "db_backup"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "TOTP_NOT_BOUND"

    from tests.test_totp_pep import sign

    _, sig = await sign(owner, "owner@example.com")
    good = sig()
    resp = await owner.post("/system/export-ticket",
                            json={"target": "db_backup", "pep_code": good["code"], "pep_word": "не-то-слово"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "RECOVERY_WORD_MISMATCH"

    resp = await owner.post("/system/export-ticket",
                            json={"target": "db_backup", "pep_code": sig()["code"], "pep_word": sig()["word"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target"] == "db_backup"
    assert body["ticket"]
    assert body["url"] == f"/system/backup?ticket={body['ticket']}"
    assert body["expires_in"] > 0


async def test_export_ticket_rejects_unknown_target(client, seeded):
    owner = await auth(client, "owner@example.com")
    resp = await owner.post("/system/export-ticket",
                            json={"target": "all_users", "pep_code": "000000", "pep_word": "голубой-кит"})
    assert resp.status_code == 422      # pydantic: pattern ^(db_backup|files_zip)$


async def test_files_zip_ticket_is_not_accepted_for_backup(client, seeded):
    """Билет именной по цели: подписали архив файлов — дамп БД по нему не скачать."""
    owner = await auth(client, "owner@example.com")
    ticket = await export_ticket(owner, "owner@example.com", "files_zip")
    resp = await owner.get("/system/backup", params={"ticket": ticket})
    assert resp.status_code == 403
    assert resp.json()["code"] == "TICKET_INVALID"


async def test_files_zip_download_by_ticket(client, seeded, db_session):
    owner = await auth(client, "owner@example.com")
    ticket = await export_ticket(owner, "owner@example.com", "files_zip")
    resp = await owner.get("/system/files-zip", params={"ticket": ticket})
    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"PK")
    # повторное предъявление того же билета отклоняется
    again = await owner.get("/system/files-zip", params={"ticket": ticket})
    assert again.status_code == 403
    assert again.json()["code"] == "TICKET_INVALID"


# ------------------------------------------------------------- WebSocket-билет
async def test_realtime_ticket_requires_auth(client, seeded):
    resp = await client.post("/realtime/ticket")          # без Authorization
    assert resp.status_code == 401

    teacher = await auth(client, "teacher1@example.com")
    resp = await teacher.post("/realtime/ticket")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket"]
    assert body["url"] == f"/realtime/ws?ticket={body['ticket']}"
    assert 0 < body["expires_in"] <= 120


class _FakeWebSocket:
    """Мини-двойник WebSocket: фиксирует accept/close и проигрывает входящие сообщения."""

    def __init__(self, incoming: list[str] | None = None, on_receive=None):
        self.incoming = list(incoming or [])
        self.on_receive = on_receive          # хук: проверить состояние, пока сокет жив
        self.accepted = False
        self.close_code: int | None = None
        self.sent: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if not self.incoming:
            raise WebSocketDisconnect(code=1000)
        message = self.incoming.pop(0)
        if self.on_receive is not None:
            self.on_receive()
        return message


async def _call_ws(ticket: str | None, token: str | None = None, incoming: list[str] | None = None,
                   on_receive=None):
    from app.api import realtime as realtime_api

    ws = _FakeWebSocket(incoming, on_receive=on_receive)
    await realtime_api.realtime_ws(ws, ticket=ticket, token=token)
    return ws


async def test_ws_accepts_valid_ticket_and_speaks_protocol(app, client, seeded):
    from app.services import realtime

    teacher = await auth(client, "teacher1@example.com")
    ticket = (await teacher.post("/realtime/ticket")).json()["ticket"]

    user_id = seeded["teacher1@example.com"]
    # пока сокет открыт, пользователь считается «онлайн» — на этом держатся push-уведомления
    seen_online: list[bool] = []
    ws = await _call_ws(ticket, incoming=["ping"], on_receive=lambda: seen_online.append(realtime.hub.online(user_id)))

    assert ws.accepted is True and ws.close_code is None
    assert ws.sent[0]["type"] == "hello"
    assert ws.sent[0]["user_id"] == user_id
    assert {"type": "pong"} in ws.sent
    assert seen_online == [True]
    # после разрыва соединение убрано из хаба (иначе события копились бы в мёртвый сокет)
    assert not realtime.hub.online(user_id)


async def test_ws_ticket_is_single_use(app, client, seeded):
    teacher = await auth(client, "teacher1@example.com")
    ticket = (await teacher.post("/realtime/ticket")).json()["ticket"]

    first = await _call_ws(ticket)
    assert first.accepted is True
    second = await _call_ws(ticket)
    assert second.accepted is False
    assert second.close_code == 4401


async def test_ws_rejects_legacy_token_in_query(app, client, seeded):
    """§5.6: access-токен в query больше не принимается — он утекал в логи nginx."""
    teacher = await auth(client, "teacher1@example.com")
    access = teacher.headers["Authorization"].split(" ", 1)[1]

    ws = await _call_ws(None, token=access)
    assert ws.accepted is False
    assert ws.close_code == 4401


async def test_ws_rejects_unknown_or_foreign_ticket(app, client, seeded):
    ws = await _call_ws("билета-с-таким-значением-не-существует")
    assert ws.accepted is False and ws.close_code == 4401

    # билет другого пользователя: hub отдаёт события только владельцу билета,
    # а чужой билет не проходит проверку «именной»
    from app.services import tickets

    other = tickets.issue(str(uuid.uuid4()), tickets.PURPOSE_WEBSOCKET)
    ws = await _call_ws(other.token)
    assert ws.accepted is False and ws.close_code == 4403     # пользователя нет в БД


async def test_ws_rejects_deactivated_user(app, client, seeded, db_session):
    """Сокет живёт долго: заблокированная учётка не должна продолжать получать push."""
    from app.models.user import User

    teacher = await auth(client, "teacher1@example.com")
    ticket = (await teacher.post("/realtime/ticket")).json()["ticket"]

    row = await db_session.get(User, uuid.UUID(seeded["teacher1@example.com"]))
    row.is_active = False
    await db_session.commit()

    ws = await _call_ws(ticket)
    assert ws.accepted is False
    assert ws.close_code == 4403

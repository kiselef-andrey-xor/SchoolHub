"""WebSocket-эндпоинт мгновенных уведомлений.

Подключение — в два шага:

    POST /api/v1/realtime/ticket          (заголовок Authorization: Bearer <access>)
        → {"ticket": "...", "expires_in": 30}
    WS   /api/v1/realtime/ws?ticket=<ticket>

Зачем не `?token=<access>`, как было раньше: браузерный WebSocket не умеет ставить
произвольные заголовки, поэтому access-токен приходилось передавать в query-строке —
а она попадает в access-логи nginx и DSM Reverse Proxy. Access-токен живёт 30 минут
и даёт полный доступ к API: утечка через лог была полноценной компрометацией сессии
(review/REVIEW.md §5.6).

Билет одноразовый (погашается при предъявлении), живёт `WS_TICKET_TTL_SEC` секунд
(по умолчанию 60 — с запасом на медленный мобильный канал) и привязан к цели
`realtime:ws`, поэтому его появление в логе бесполезно для атакующего.
См. app/services/tickets.py.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, WebSocket
from pydantic import BaseModel
from sqlalchemy import select

from app.models.user import User
from app.security import CurrentUser
from app.services import realtime, tickets

router = APIRouter(prefix="/realtime", tags=["realtime"])

CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403

# Тесты подменяют фабрику сессий (тот же приём, что в app/services/audit.py —
# см. tests/conftest.py::app). В проде None: берётся штатная SessionLocal.
session_factory_override = None


def _session():
    """Сессия БД вне механизма зависимостей FastAPI.

    WebSocket-роут не получает SessionDep: соединение живёт минуты и часы,
    а сессию нужно открыть на одну короткую проверку и сразу закрыть.
    """
    if session_factory_override is not None:
        return session_factory_override()
    from app.database import SessionLocal

    return SessionLocal()


class TicketOut(BaseModel):
    ticket: str
    expires_in: int
    url: str          # готовый адрес подключения — клиент подставляет в new WebSocket(...)


@router.post("/ticket", response_model=TicketOut, summary="Одноразовый билет для WebSocket-подключения")
async def issue_ticket(actor: CurrentUser) -> TicketOut:
    """Выдаёт билет на ОДНО подключение. Требует обычный access-токен в заголовке,
    поэтому сам по себе защищён RBAC и не светит токеном в URL."""
    ticket = tickets.issue(str(actor.id), tickets.PURPOSE_WEBSOCKET)
    return TicketOut(ticket=ticket.token, expires_in=ticket.expires_in,
                     url=f"/realtime/ws?ticket={ticket.token}")


@router.websocket("/ws")
async def realtime_ws(
    ws: WebSocket,
    ticket: str = Query(description="одноразовый билет из POST /realtime/ticket"),
    token: str | None = Query(default=None, description="УСТАРЕЛО: access-токен. Не используется"),
) -> None:
    if token and not ticket:
        # явный отказ вместо тихого приёма: старый клиент должен получить понятную
        # причину и обновиться, а не работать по незащищённой схеме
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return

    user_id = tickets.peek(ticket, tickets.PURPOSE_WEBSOCKET)
    if not user_id:
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return
    # погашаем только после успешной проверки — иначе билет сгорит на ошибке валидации
    tickets.consume(ticket, tickets.PURPOSE_WEBSOCKET, user_id)

    # Билет подтверждает, что access-токен был валиден минуту назад, но сокет
    # живёт долго: проверяем, что учётка всё ещё существует и активна. Без этого
    # заблокированный или удалённый пользователь продолжал бы получать push до
    # истечения access-токена (review/REVIEW.md §5.6).
    async with _session() as session:
        user = (await session.execute(select(User).where(User.id == uuid.UUID(user_id)))).scalar_one_or_none()
        if user is None or not user.is_active or user.is_deleted:
            await ws.close(code=CLOSE_FORBIDDEN)
            return

    await ws.accept()
    realtime.hub.connect(user_id, ws)
    try:
        await ws.send_json({"type": "hello", "user_id": user_id})
        while True:
            message = await ws.receive_text()
            if message == "ping":
                await ws.send_json({"type": "pong"})
    except Exception:  # noqa: BLE001 — обрыв соединения штатен для WS
        pass
    finally:
        realtime.hub.disconnect(user_id, ws)

"""WebSocket-шлюз мгновенных уведомлений.

Каждая вкладка браузера держит одно соединение `/api/v1/realtime/ws?ticket=<одноразовый>`;
сервис уведомлений после записи в БД рассылает событие в сокет пользователя —
тост «Вам поставили новую задачу» появляется без перезагрузки и без поллинга.

Ограничения (осознанные):
  * реестр соединений живёт в памяти процесса: при нескольких worker uvicorn
    событие уйдёт только в соединения своего worker. Поэтому `--workers 1` в
    docker-compose.yml — ОБЯЗАТЕЛЬНОЕ требование, а не экономия памяти:
    увеличение числа воркеров молча сломает уведомления без единой ошибки в логах;
  * соединение аутентифируется ОДНОРАЗОВЫМ билетом из query (браузер не умеет
    ставить заголовки в WS). Access-токен в query больше не передаётся — он попадал
    бы в access-логи nginx. См. app/services/tickets.py и app/api/realtime.py;
  * клиент держит fallback: прежний поллинг счётчика непрочитанных (45 c) остаётся.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("schoolhub.realtime")


class Hub:
    """Реестр открытых сокетов: user_id -> множество соединений (вкладки)."""

    def __init__(self) -> None:
        self.conns: dict[str, set[WebSocket]] = {}

    def connect(self, user_id: str, ws: WebSocket) -> None:
        self.conns.setdefault(user_id, set()).add(ws)
        logger.debug("realtime: connect user=%s total=%s", user_id, len(self.conns[user_id]))

    def disconnect(self, user_id: str, ws: WebSocket) -> None:
        conns = self.conns.get(user_id)
        if conns is None:
            return
        conns.discard(ws)
        if not conns:
            self.conns.pop(user_id, None)

    def online(self, user_id: str) -> bool:
        return bool(self.conns.get(user_id))

    async def send(self, user_id: str, message: dict[str, Any]) -> int:
        """Шлёт сообщение во все сокеты пользователя; возвращает число доставок."""
        conns = list(self.conns.get(user_id) or ())
        sent = 0
        for ws in conns:
            try:
                await ws.send_json(message)
                sent += 1
            except Exception:                     # сокет умер — уберём в disconnect
                self.disconnect(user_id, ws)
        return sent


hub = Hub()


async def notify_user(user_id, message: dict[str, Any]) -> int:
    return await hub.send(str(user_id), message)

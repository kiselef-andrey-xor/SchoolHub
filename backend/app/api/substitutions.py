"""API замещений (модуль 3 ТЗ): журнал, создание администрацией, клик-подтверждение, CSV.

Права (v1.11):
  * журнал ЦЕЛИКОМ — Владелец, администрация и «кадровик» (грант Владельца
    `users.substitutions_access`); остальные сотрудники получают только СВОИ
    замещения (как отсутствующий или замещающий) — без этого замещающий не смог
    бы подтвердить принятие, а передача объектов выполняется именно после него;
  * оформление / продление / завершение / отмена — администрация и Владелец;
  * удаление строк — Владелец и кадровик (`DELETE /substitutions/{id}`).
"""
from __future__ import annotations

import io
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.csvsafe import safe_writer
from app.enums import Role, SubstitutionStatus
from app.errors import forbidden
from app.models.user import User
from app.schemas.journal import (
    SubstitutionCancelIn,
    SubstitutionExtendIn,
    SubstitutionIn,
    SubstitutionOut,
)
from app.security import CurrentUser, SessionDep, require_role
from app.services import lockdown
from app.services import substitutions as service

router = APIRouter(prefix="/substitutions", tags=["substitutions"])

ManagerDep = Annotated[User, Depends(require_role(Role.ADMIN))]


async def _journal_reader(session: SessionDep, actor: CurrentUser) -> User:
    """Журнал целиком: администрация, Владелец и кадровик (грант Владельца).

    Остальные — 403: экспортировать чужие замещения сотруднику не за чем, а свои
    он видит в `GET /substitutions` и в уведомлении.
    """
    if service.sees_full_journal(actor):
        return actor
    raise forbidden("Журнал замещений доступен администрации, Владельцу и кадровику")


JournalDep = Annotated[User, Depends(_journal_reader)]

_STATUS_RU = {
    SubstitutionStatus.ACTIVE.value: "Активно",
    SubstitutionStatus.CONFIRMED.value: "Подтверждено замещающим",
    SubstitutionStatus.ENDED.value: "Завершено",
    SubstitutionStatus.CANCELLED.value: "Отменено",
}


@router.post("", response_model=SubstitutionOut, status_code=201,
             summary="Создать замещение (администрация); объекты передаются после подтверждения замещающим")
async def create_substitution(payload: SubstitutionIn, session: SessionDep, actor: ManagerDep) -> SubstitutionOut:
    sub = await service.create_sub(
        session, actor,
        absent_id=payload.absent_user_id, substitute_id=payload.substitute_user_id,
        reason=payload.reason, start=payload.start_date, end=payload.end_date,
        transfer_tasks=payload.transfer_tasks, transfer_plans=payload.transfer_plans,
        transfer_vshk=payload.transfer_vshk,
    )
    await session.commit()
    await session.refresh(sub)
    return SubstitutionOut.model_validate(sub)


@router.get("", response_model=list[SubstitutionOut],
            summary="Журнал замещений (целиком — администрация/Владелец/кадровик; остальным — только свои)")
async def list_substitutions(
    session: SessionDep,
    actor: CurrentUser,
    status: str | None = Query(default=None, max_length=20),
    user_id: uuid.UUID | None = None,
    ongoing: bool | None = None,
) -> list[SubstitutionOut]:
    rows = await service.list_subs(session, actor, status=status, user_id=user_id, ongoing=ongoing)
    return [SubstitutionOut.model_validate(r) for r in rows]


@router.get("/export.csv", summary="Экспорт журнала замещений в CSV (отчётность)")
async def export_substitutions(
    session: SessionDep, actor: JournalDep,
    status: str | None = None, user_id: uuid.UUID | None = None,
) -> Response:
    rows = await service.list_subs(session, actor, status=status, user_id=user_id)
    buf = io.StringIO()
    w = safe_writer(buf, delimiter=";")
    w.writerow(["Отсутствующий", "Замещающий", "Причина", "Начало", "Конец",
                "Передать задачи", "Передать планы", "Передать ВШК", "Статус",
                "Подтверждено", "Передано замещающему", "Завершено", "Инициатор", "Сводка"])
    for s in rows:
        w.writerow([
            # ФИО — через lockdown.disp(): CSV строится мимо Pydantic-схем (review §P1-1)
            lockdown.disp(s.absent_user.full_name, s.absent_user_id),
            lockdown.disp(s.substitute_user.full_name, s.substitute_user_id), s.reason,
            f"{s.start_date:%d.%m.%Y}", f"{s.end_date:%d.%m.%Y}" if s.end_date else "",
            "да" if s.transfer_tasks else "нет", "да" if s.transfer_plans else "нет",
            "да" if s.transfer_vshk else "нет",
            _STATUS_RU.get(str(s.status.value), str(s.status.value)),
            f"{s.confirmed_at:%d.%m.%Y %H:%M}" if s.confirmed_at else "",
            f"{s.transferred_at:%d.%m.%Y %H:%M}" if s.transferred_at else "",
            f"{s.ended_at:%d.%m.%Y %H:%M}" if s.ended_at else "",
            lockdown.disp(s.initiator.full_name if s.initiator else None, s.initiator_id),
            s.note or "",
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="substitutions.csv"'},
    )


class ConfirmBody(BaseModel):
    # подтверждение — простой клик: тело не нужно; схема — для совместимости с опциональным комментарием
    comment: str | None = Field(default=None, max_length=500)


@router.post("/{sub_id}/confirm", response_model=SubstitutionOut,
             summary="Подтвердить принятие (простой клик, только замещающий) — передаёт объекты")
async def confirm(sub_id: uuid.UUID, session: SessionDep, actor: CurrentUser,
                  payload: ConfirmBody | None = None) -> SubstitutionOut:
    sub = await service.get_sub(session, sub_id, actor)
    await service.confirm_sub(session, actor, sub)
    await session.commit()
    await session.refresh(sub)
    return SubstitutionOut.model_validate(sub)


@router.post("/{sub_id}/end", response_model=SubstitutionOut, summary="Завершить замещение (администрация)")
async def end(sub_id: uuid.UUID, session: SessionDep, actor: ManagerDep) -> SubstitutionOut:
    sub = await service.get_sub(session, sub_id, actor)
    await service.end_sub(session, actor, sub)
    await session.commit()
    await session.refresh(sub)
    return SubstitutionOut.model_validate(sub)


@router.post("/{sub_id}/cancel", response_model=SubstitutionOut, summary="Отменить замещение (администрация)")
async def cancel(sub_id: uuid.UUID, payload: SubstitutionCancelIn, session: SessionDep,
                 actor: ManagerDep) -> SubstitutionOut:
    sub = await service.get_sub(session, sub_id, actor)
    await service.cancel_sub(session, actor, sub, payload.comment)
    await session.commit()
    await session.refresh(sub)
    return SubstitutionOut.model_validate(sub)


@router.post("/{sub_id}/extend", response_model=SubstitutionOut,
             summary="Продлить период замещения (администрация) — например, если больничный продлили")
async def extend(sub_id: uuid.UUID, payload: SubstitutionExtendIn, session: SessionDep,
                 actor: ManagerDep) -> SubstitutionOut:
    sub = await service.get_sub(session, sub_id, actor)
    await service.extend_sub(session, actor, sub, new_end=payload.end_date, comment=payload.comment)
    await session.commit()
    await session.refresh(sub)
    return SubstitutionOut.model_validate(sub)


@router.delete("/{sub_id}", status_code=204,
               summary="Удалить строку замещения (Владелец или назначенный им кадровик)")
async def delete(sub_id: uuid.UUID, session: SessionDep, actor: CurrentUser) -> None:
    sub = await service.get_sub(session, sub_id, actor)
    await service.delete_sub(session, actor, sub)
    await session.commit()

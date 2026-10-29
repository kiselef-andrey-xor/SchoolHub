"""Сервис модуля «Приказы».

Ответственности:
  * CRUD + жизненный цикл (DRAFT → PUBLISHED → ARCHIVED)
  * версионирование файлов (PDF/DOCX)
  * круг ознакомления и подтверждение (с метаданными)
  * фильтрация/пагинация
  * row-level scoping: TEACHER видит только PUBLISHED, staff — всё
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Sequence

from fastapi import UploadFile
from sqlalchemy import Select, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dbutils import ilike
from app.enums import AckStatus, OrderStatus, Role, SignaturePolicy
from app.errors import bad_request, conflict, forbidden, not_found
from app.models.notification import Notification
from app.models.order import Order, OrderAcknowledgement, OrderFile
from app.models.user import User
from app.schemas.order import OrderCreateIn, OrderUpdateIn
from app.services import audit, notifications, totp
from app.services import storage as _storage_mod
from app.services.storage import StoredFile
from app.types import enum_value

MAX_FILE_VERSIONS = 20


def _s(value: Any) -> str:
    """Нормализация значения enum-колонки (Enum | str) к строке."""
    return enum_value(value)


# ------------------------------------------------------------------ queries
def _base_select() -> Select:
    return (
        select(Order)
        .options(
            selectinload(Order.files).selectinload(OrderFile.uploaded_by),
            selectinload(Order.acknowledgements).selectinload(OrderAcknowledgement.user),
            selectinload(Order.created_by),
        )
    )


async def get_order(session: AsyncSession, order_id: uuid.UUID, actor: User) -> Order:
    stmt = _base_select().where(Order.id == order_id)
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise not_found("Приказ")
    # Row-level access: черновики и архивные метаданные недоступны учителю,
    # если приказ не опубликован.
    if not can_manage(actor) and order.status != OrderStatus.PUBLISHED:
        raise forbidden("Приказ ещё не опубликован")
    return order


async def list_orders(
    session: AsyncSession,
    actor: User,
    *,
    search: str | None = None,
    order_type: str | None = None,
    status: OrderStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    ack: str | None = None,          # None | all | pending | acknowledged  (для учителя — свои)
    mine_pending: bool = False,
    page: int = 1,
    per_page: int = 20,
) -> tuple[Sequence[Order], int]:
    stmt = _base_select()
    where = []

    if not can_manage(actor):
        where.append(Order.status == OrderStatus.PUBLISHED)
    elif status is not None:
        where.append(Order.status == status)

    if order_type is not None:
        where.append(Order.order_type == order_type)
    if date_from:
        where.append(Order.issued_on >= date_from)
    if date_to:
        where.append(Order.issued_on <= date_to)
    if search:
        where.append(
            or_(ilike(Order.title, search), ilike(Order.number, search), ilike(Order.description, search))
        )

    if ack in ("pending", "acknowledged") or mine_pending:
        target_status = AckStatus.PENDING.value if (mine_pending or ack == "pending") else AckStatus.ACKNOWLEDGED.value
        if can_manage(actor) and not mine_pending:
            # админ: приказы, где есть хоть одно неподтверждённое ознакомление
            sub = select(OrderAcknowledgement.order_id).where(
                OrderAcknowledgement.status == AckStatus(target_status)
            )
        else:
            # учитель: только свои записи листа ознакомления
            sub = select(OrderAcknowledgement.order_id).where(
                and_(
                    OrderAcknowledgement.user_id == actor.id,
                    OrderAcknowledgement.status == AckStatus(target_status),
                )
            )
        where.append(Order.id.in_(sub))

    if where:
        stmt = stmt.where(and_(*where))

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(Order.issued_on.desc(), Order.created_at.desc())
    stmt = stmt.limit(per_page).offset((page - 1) * per_page)
    rows = (await session.execute(stmt)).scalars().unique().all()
    return rows, int(total)


# ------------------------------------------------------------------- create
async def _assert_type_exists(session: AsyncSession, code: str) -> None:
    from app.models.order_type import OrderTypeRow

    if await session.get(OrderTypeRow, code) is None:
        raise bad_request(f"Неизвестный тип приказа: {code}")


async def create_order(session: AsyncSession, actor: User, data: OrderCreateIn) -> Order:
    ensure_manager(actor)
    await _assert_type_exists(session, data.order_type)
    await _ensure_number_free(session, data.number, data.order_year)

    order = Order(
        number=data.number.strip(),
        order_year=data.order_year,
        title=data.title.strip(),
        description=data.description,
        order_type=data.order_type,
        status=OrderStatus.DRAFT,
        issued_on=data.issued_on,
        ack_deadline=data.ack_deadline,
        require_all=data.require_all,
        recipient_ids=(
            [str(x) for x in await _validate_recipient_ids(session, data.recipient_ids)]
            if (not data.require_all and data.recipient_ids) else None
        ),
        signature_policy=_resolve_policy(data.order_type, data.signature_policy),
        created_by_id=actor.id,
    )
    session.add(order)
    await session.flush()

    await audit.write_audit(
        session,
        actor=actor,
        action="ORDER_CREATE",
        entity_type="order",
        entity_id=order.id,
        changes={"number": order.number, "title": order.title, "type": order.order_type},
    )
    return order


async def update_order(session: AsyncSession, actor: User, order: Order, data: OrderUpdateIn) -> Order:
    ensure_manager(actor)
    if order.status == OrderStatus.PUBLISHED:
        raise conflict("Приказ опубликован — изменение недоступно, только ознакомление")
    if order.status == OrderStatus.ARCHIVED:
        raise conflict("Приказ в архиве — редактирование недоступно")

    before = {
        "number": order.number,
        "order_year": order.order_year,
        "title": order.title,
        "description": order.description,
        "order_type": order.order_type,
        "issued_on": order.issued_on,
        "ack_deadline": order.ack_deadline,
        "signature_policy": order.signature_policy,
        "status": enum_value(order.status),
    }

    patch = data.model_dump(exclude_unset=True)
    if patch.get("order_type"):
        await _assert_type_exists(session, patch["order_type"])

    # смена статуса через PATCH — только допустимые переходы
    if "status" in patch and patch["status"] is not None:
        new_status = OrderStatus(patch["status"]) if not isinstance(patch["status"], OrderStatus) else patch["status"]
        _assert_transition(order.status, new_status)
        if new_status == OrderStatus.PUBLISHED:
            await publish_order(session, actor, order, notify=False)
            patch.pop("status")
        elif new_status == OrderStatus.ARCHIVED:
            order.status = OrderStatus.ARCHIVED
        else:
            order.status = new_status

    if ("number" in patch or "order_year" in patch) and patch.get("number"):
        await _ensure_number_free(
            session,
            patch.get("number") or order.number,
            patch.get("order_year") or order.order_year,
            exclude_id=order.id,
        )

    if "recipient_ids" in patch:
        raw = patch.pop("recipient_ids")
        if raw is not None:
            ids = [uuid.UUID(str(x)) for x in raw]
            patch["recipient_ids"] = [str(x) for x in await _validate_recipient_ids(session, ids)]
    if patch.get("require_all") is True:
        patch["recipient_ids"] = None

    for field, value in patch.items():
        if field == "status":
            continue
        if field in {"issued_on", "ack_deadline"} and isinstance(value, str):
            value = date.fromisoformat(value)
        setattr(order, field, value)

    await session.flush()

    # автоправило «тип приказа → политика»: при смене типа пересчитываем политику,
    # если она не была выставлена вручную на уровень выше автоматической
    if "order_type" in patch or "signature_policy" in patch:
        order.signature_policy = _resolve_policy(
            order.order_type, patch.get("signature_policy", order.signature_policy)
        )

    after = {
        "number": order.number,
        "order_year": order.order_year,
        "title": order.title,
        "description": order.description,
        "order_type": order.order_type,
        "issued_on": order.issued_on,
        "ack_deadline": order.ack_deadline,
        "signature_policy": order.signature_policy,
        "status": enum_value(order.status),
    }
    await audit.write_audit(
        session,
        actor=actor,
        action="ORDER_UPDATE",
        entity_type="order",
        entity_id=order.id,
        changes=audit.diff(before, after),
    )
    return order


def _assert_transition(current: OrderStatus | str, target: OrderStatus) -> None:
    current = OrderStatus(current) if isinstance(current, str) else current
    allowed = {
        OrderStatus.DRAFT: {OrderStatus.PUBLISHED, OrderStatus.ARCHIVED},
        OrderStatus.PUBLISHED: {OrderStatus.ARCHIVED},
        OrderStatus.ARCHIVED: set(),
    }[current]
    if target not in allowed:
        raise conflict(f"Недопустимый переход статуса: {current.value} → {target.value}")


# -------------------------------------------------------------------- files
async def save_file_version(session: AsyncSession, actor: User, order: Order, upload: UploadFile) -> OrderFile:
    ensure_manager(actor)
    if order.status == OrderStatus.PUBLISHED:
        raise conflict("Приказ опубликован — заменять документ нельзя, только ознакомиться")
    if order.status == OrderStatus.ARCHIVED:
        raise conflict("Приказ в архиве — загрузка файлов недоступна")

    filename = upload.filename or "document"
    mime = upload.content_type or ""
    stored: StoredFile = await _storage_mod.storage.save(upload.file, filename, mime)

    # предыдущие версии помечаем не-текущими (история сохраняется)
    for f in order.files:
        f.is_current = False

    next_version = max((f.version for f in order.files), default=0) + 1
    if next_version > MAX_FILE_VERSIONS:
        raise conflict("Слишком много версий файла — архивируйте приказ и создайте новый")

    file_obj = OrderFile(
        order_id=order.id,
        storage_path=stored.storage_path,
        original_name=stored.original_name,
        mime_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        checksum=stored.checksum,
        version=next_version,
        is_current=True,
        uploaded_by_id=actor.id,
    )
    session.add(file_obj)

    if order.status == OrderStatus.PUBLISHED:
        # опубликованный приказ изменён → уведомляем круг ознакомления
        await notifications.push_many(
            session,
            user_ids=[a.user_id for a in order.acknowledgements if _s(a.status) == AckStatus.PENDING.value],
            type_=notifications.ORDER_UPDATED,
            title=f"Обновлён документ приказа №{order.number}",
            body=f"Загружена версия {next_version}: {stored.original_name}",
            entity_type="order",
            entity_id=order.id,
        )

    await session.flush()
    await audit.write_audit(
        session,
        actor=actor,
        action="ORDER_FILE_UPLOAD",
        entity_type="order",
        entity_id=order.id,
        changes={
            "file": stored.original_name,
            "version": next_version,
            "size": stored.size_bytes,
            "sha256": stored.checksum,
        },
    )
    return file_obj


async def delete_file(session: AsyncSession, actor: User, order: Order, file_id: uuid.UUID) -> None:
    ensure_manager(actor)
    if order.status != OrderStatus.DRAFT:
        raise conflict("Удалять файлы можно только у черновика. Для опубликованного приказа загрузите новую версию.")
    target = next((f for f in order.files if f.id == file_id), None)
    if target is None:
        raise not_found("Файл")

    order.files.remove(target)
    await session.delete(target)
    await session.flush()
    _storage_mod.storage.delete(target.storage_path)

    if order.files:
        order.files[0].is_current = True
    await audit.write_audit(
        session, actor=actor, action="ORDER_FILE_DELETE", entity_type="order", entity_id=order.id,
        changes={"file": target.original_name},
    )


# ------------------------------------------------------------------ publish
async def publish_order(
    session: AsyncSession,
    actor: User,
    order: Order,
    *,
    notify: bool = True,
    recipient_ids: list[uuid.UUID] | None = None,
) -> Order:
    ensure_manager(actor)
    _assert_transition(order.status, OrderStatus.PUBLISHED)

    if not order.files:
        raise conflict("Нельзя опубликовать приказ без загруженного документа (PDF/DOCX)")

    order.status = OrderStatus.PUBLISHED
    order.published_at = datetime.now(timezone.utc)

    recipients = await _resolve_recipients(session, order, recipient_ids)
    if recipient_ids:
        order.require_all = False
        order.recipient_ids = [str(u.id) for u in recipients]
    await sync_acknowledgements(session, order, recipients)

    if notify and recipients:
        await notifications.notify_order_published(session, order, recipients)

    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="ORDER_PUBLISH", entity_type="order", entity_id=order.id,
        changes={"recipients": len(recipients)},
    )
    return order


async def archive_order(session: AsyncSession, actor: User, order: Order) -> Order:
    ensure_manager(actor)
    _assert_transition(order.status, OrderStatus.ARCHIVED)
    order.status = OrderStatus.ARCHIVED
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="ORDER_ARCHIVE", entity_type="order", entity_id=order.id,
    )
    return order


async def delete_order(session: AsyncSession, actor: User, order: Order) -> None:
    """Черновик удаляет ADMIN/OWNER; архивный приказ — только OWNER (безвозвратно,
    вместе с документом и листом ознакомления: ознакомления каскадно удаляются)."""
    ensure_manager(actor)
    if order.status == OrderStatus.ARCHIVED:
        if actor.role != Role.OWNER:
            raise forbidden("Удалять архивные приказы может только Владелец")
    elif order.status != OrderStatus.DRAFT:
        raise conflict("Удалить можно только черновик. Опубликованный приказ отправляется в архив.")
    files = list(order.files)
    ack_count = len(order.acknowledgements) if order.acknowledgements else 0
    await session.delete(order)
    await session.execute(
        delete(Notification).where(Notification.entity_type == "order", Notification.entity_id == order.id)
    )
    await session.flush()
    for f in files:
        _storage_mod.storage.delete(f.storage_path)
    await audit.write_audit(
        session, actor=actor, action="ORDER_DELETE", entity_type="order", entity_id=order.id,
        changes={
            "number": order.number,
            "title": order.title,
            "status": enum_value(order.status),
            "ack_records_deleted": ack_count,
        },
    )


# ------------------------------------------------------------- acknowledgements
async def _validate_recipient_ids(session: AsyncSession, ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Проверяет, что все указанные сотрудники существуют и активны."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    rows = await session.execute(select(User).where(User.id.in_(unique), User.is_active.is_(True)))
    found = {u.id for u in rows.scalars().all()}
    missing = [str(x) for x in unique if x not in found]
    if missing:
        raise bad_request("Часть получателей не найдена или заблокированы")
    return unique


async def _resolve_recipients(
    session: AsyncSession, order: Order, explicit_ids: list[uuid.UUID] | None
) -> list[User]:
    if explicit_ids:
        rows = await session.execute(
            select(User).where(User.id.in_(explicit_ids), User.is_active.is_(True))
        )
        return list(rows.scalars().all())

    if not order.require_all and order.recipient_ids:
        # выбранный круг сохранён на приказе (черновик или смена круга после публикации)
        ids = [uuid.UUID(str(x)) for x in order.recipient_ids]
        rows = await session.execute(
            select(User).where(User.id.in_(ids), User.is_active.is_(True)).order_by(User.full_name)
        )
        return list(rows.scalars().all())

    stmt = select(User).where(User.is_active.is_(True))
    if order.require_all:
        # «ознакомить всех» = все активные сотрудники, кроме владельца
        stmt = stmt.where(User.role != Role.OWNER.value)
    else:
        stmt = stmt.where(User.role == Role.TEACHER.value)
    return list((await session.execute(stmt.order_by(User.full_name))).scalars().all())


async def sync_acknowledgements(session: AsyncSession, order: Order, recipients: Sequence[User]) -> None:
    """Приводит лист ознакомления в соответствие со списком получателей (идемпотентно)."""
    existing = {a.user_id: a for a in order.acknowledgements}
    target_ids = {u.id for u in recipients}

    removed = 0
    for uid, ack in list(existing.items()):
        if uid not in target_ids and _s(ack.status) == AckStatus.PENDING.value:
            await session.delete(ack)               # DELETE первым — иначе cascade мешает
            if ack in order.acknowledgements:
                order.acknowledgements.remove(ack)
            removed += 1
    if removed:
        await session.flush()

    for user in recipients:
        if user.id not in existing:
            session.add(
                OrderAcknowledgement(
                    order_id=order.id,
                    user_id=user.id,
                    status=AckStatus.PENDING,
                )
            )
    await session.flush()


async def set_recipients(session: AsyncSession, actor: User, order: Order, user_ids: list[uuid.UUID]) -> Order:
    ensure_manager(actor)
    if order.status == OrderStatus.ARCHIVED:
        raise conflict("Приказ в архиве")
    if order.status == OrderStatus.DRAFT:
        raise conflict("Сначала опубликуйте приказ")
    recipients = await _resolve_recipients(session, order, user_ids)
    if not recipients:
        raise bad_request("Не найдено ни одного активного получателя")
    order.require_all = False
    order.recipient_ids = [str(u.id) for u in recipients]
    await sync_acknowledgements(session, order, recipients)
    await notifications.notify_order_published(session, order, recipients)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="ORDER_RECIPIENTS", entity_type="order", entity_id=order.id,
        changes={"recipients": [str(u.id) for u in recipients]},
    )
    return order


async def acknowledge(
    session: AsyncSession,
    actor: User,
    order: Order,
    *,
    comment: str | None,
    ip: str | None,
    user_agent: str | None,
    code: str | None = None,
    word: str | None = None,
) -> OrderAcknowledgement:
    """Подтверждение ознакомления. Повторное подтверждение невозможно (409)."""
    if order.status != OrderStatus.PUBLISHED:
        raise conflict("Ознакомиться можно только с опубликованным приказом")

    ack = next((a for a in order.acknowledgements if a.user_id == actor.id), None)
    if ack is None:
        if not can_manage(actor):
            # Учитель НЕ входит в круг ознакомления этого приказа — подтверждение невозможно.
            raise conflict("Вы не входите в список ознакомления по этому приказу")
        # Админ/владелец может не входить в круг — фиксируем факт прочтения «для себя».
        ack = OrderAcknowledgement(order_id=order.id, user_id=actor.id, status=AckStatus.PENDING)
        session.add(ack)
        order.acknowledgements.append(ack)

    if _s(ack.status) == AckStatus.ACKNOWLEDGED.value:
        raise conflict(f"Ознакомление уже подтверждено {ack.acknowledged_at:%d.%m.%Y %H:%M}")

    # ПЭП: проверяем требуемый уровень подписи ДО фиксации ознакомления.
    # Нет привязки TOTP, а документ требует код — блокируем с подсказкой (без фолбэка на пароль).
    policy = totp.effective_policy(order)
    sign_method = await totp.enforce_signature(session, actor, policy, code, word)

    ack.status = AckStatus.ACKNOWLEDGED
    ack.acknowledged_at = datetime.now(timezone.utc)
    ack.sign_method = sign_method.value
    ack.comment = comment
    ack.ip_address = ip
    ack.user_agent = user_agent
    await session.flush()

    await audit.write_audit(
        session, actor=actor, action="ORDER_ACK", entity_type="order", entity_id=order.id,
        changes={
            "order_number": order.number, "comment": comment, "ip": ip,
            "policy": policy, "sign_method": sign_method.value,
        },
    )
    # постановщику — сигнал, что приказ прочитан
    if order.created_by_id and order.created_by_id != actor.id:
        await notifications.push(
            session,
            user_id=order.created_by_id,
            type_=notifications.ORDER_UPDATED,
            title=f"{actor.full_name} ознакомлен(а) с приказом №{order.number}",
            body=order.title,
            entity_type="order",
            entity_id=order.id,
        )
    return ack


# -------------------------------------------------------------------- helpers
def _resolve_policy(order_type: str, requested: str | None) -> str:
    """Политика подписи при создании/правке приказа.

    Автоправило «тип приказа → политика»: для типов из ORDER_TYPE_SIGNATURE_POLICY
    (движение обучающихся — всегда усиленная) запрошенная политика НЕ может быть
    ниже автоматической; более высокий уровень разрешён.
    """
    from app.enums import ORDER_TYPE_SIGNATURE_POLICY

    auto = ORDER_TYPE_SIGNATURE_POLICY.get(str(order_type or ""))
    base = (requested or SignaturePolicy.CLICK.value)
    if base not in {e.value for e in SignaturePolicy}:
        raise bad_request(f"Неизвестная политика подписи: {base}")
    if auto and totp.policy_level(auto) > totp.policy_level(base):
        return auto
    return base


def can_manage(actor: User) -> bool:
    """Право управлять приказами: Владелец и Администрация."""
    return actor.role in (Role.OWNER, Role.ADMIN)


def ensure_manager(actor: User) -> None:
    if not can_manage(actor):
        raise forbidden("Управление приказами доступно Владельцу и Администрации")


async def _ensure_number_free(
    session: AsyncSession, number: str, year: int, exclude_id: uuid.UUID | None = None
) -> None:
    stmt = select(func.count()).select_from(Order).where(
        Order.number == number.strip(), Order.order_year == year
    )
    if exclude_id:
        stmt = stmt.where(Order.id != exclude_id)
    if (await session.execute(stmt)).scalar_one() > 0:
        raise conflict(f"Приказ №{number} за {year} год уже существует")


def my_ack(order: Order, actor: User) -> OrderAcknowledgement | None:
    return next((a for a in order.acknowledgements if a.user_id == actor.id), None)


def ack_of_status(order: Order, status: AckStatus) -> list[OrderAcknowledgement]:
    return [a for a in order.acknowledgements if _s(a.status) == status.value]


def to_dict(order: Order) -> dict[str, Any]:  # pragma: no cover - утилита для отчётов
    return {
        "id": str(order.id),
        "number": order.number,
        "title": order.title,
        "type": order.order_type,
        "status": enum_value(order.status),
        "issued_on": order.issued_on.isoformat(),
        "ack_total": order.ack_total,
        "ack_done": order.ack_done,
    }

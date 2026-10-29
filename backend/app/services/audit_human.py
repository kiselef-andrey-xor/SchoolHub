"""Человекопонятное представление записей журнала событий.

Сырой аудит хранит technical details (diff по полям, uuid, sha256). Для экрана
«Журнал событий» каждая запись превращается в:
  * summary — одна фраза человеческим языком («Создан приказ № 433 «О зачислении…»»);
  * lines   — список коротких строк с деталями («Номер: 432 → 433»,
              «Круг ознакомления (3): Иванова А. А., …»).

Все uuid подставляются именами (сотрудники/должности), коды ролей и статусов —
русскими названиями, даты — в формате дд.мм.гггг. Необязательные технические
поля (sha256, ip для не-входов, jti и т. п.) в человекопонятный вид не попадают.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.equipment import EquipmentItem
from app.models.incoming import IncomingLetter
from app.models.movement import MovementOrder
from app.models.order import Order
from app.models.plan import Plan
from app.models.position import Position
from app.models.task import Task
from app.models.user import User
from app.models.vshk import VshkItem, VshkPlan

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")

ROLE_RU = {"OWNER": "владелец", "ADMIN": "администрация", "TEACHER": "учитель"}
STATUS_RU = {
    "DRAFT": "черновик", "PUBLISHED": "опубликован", "ARCHIVED": "в архиве",
    "NEW": "новая", "IN_PROGRESS": "в работе", "DONE": "выполнена",
    "PLANNED": "запланировано", "IN_WORK": "проводится",
    "PENDING": "ожидает ознакомления", "ACKNOWLEDGED": "ознакомлен",
    "ACTIVE": "активна", "OVERDUE": "просрочена",
}
PRIORITY_RU = {"LOW": "низкий", "NORMAL": "обычный", "HIGH": "высокий", "URGENT": "срочный"}
SIGN_RU = {
    "NONE": "без подписи (аудит)",
    "CLICK": "клик «Ознакомлен»",
    "TOTP": "код аутентификатора",
    "CODE_WORD": "код + контрольное слово",
}
SOURCE_RU = {
    "local": "локальная учётная запись", "ad": "доменная учётная запись (AD)",
    "ldap": "доменная учётная запись (AD)", "manual": "вручную", "task": "из задачи",
}
ORDER_TYPE_RU = {
    "HR": "по личному составу", "STUDENTS": "по обучающимся", "EDUCATION": "по учебной деятельности",
    "SAFETY": "по охране труда и безопасности", "ADMIN": "административно-хозяйственный",
    "OTHER": "прочий",
}
MOVEMENT_TYPE_RU = {"ENROLLMENT": "зачисление", "TRANSFER": "перевод", "EXPULSION": "отчисление"}

FIELD_RU = {
    "number": "номер", "order_year": "учебный год", "title": "название", "description": "описание",
    "order_type": "тип приказа", "type": "тип", "status": "статус", "issued_on": "дата приказа",
    "ack_deadline": "срок ознакомления", "deadline": "срок", "priority": "приоритет",
    "full_name": "ФИО", "role": "роль", "email": "e-mail", "login": "логин", "phone": "телефон",
    "is_active": "учётная запись активна", "positions": "должности", "position": "должность",
    "file": "файл", "version": "версия", "size": "размер", "recipients": "круг ознакомления",
    "assignees": "исполнители", "comment": "комментарий", "order_number": "№ приказа",
    "reg": "рег. номер", "reg_no": "рег. номер", "reg_year": "рег. год", "reg_manual": "рег. номер указан вручную",
    "incoming_number": "№ входящего", "incoming_date": "дата входящего", "sent_date": "дата отправки",
    "addressee": "адресат", "subject": "тема", "note": "примечание", "responsible_id": "ответственный",
    "source": "источник", "reason": "причина", "week": "неделя", "events": "мероприятий",
    "responsibles": "ответственных", "notified": "уведомлено", "items": "мероприятий",
    "year": "учебный год", "month": "месяц", "section": "направление контроля",
    "control_type": "вид контроля", "resp": "ответственные", "summary": "срок и форма итогов",
    "result_text": "итог", "result_file": "файл итога", "result_file_name": "файл итога",
    "name": "наименование", "inventory_number": "инв. номер", "serial_number": "серийный номер",
    "quantity": "количество", "location": "расположение", "student": "обучающийся",
    "children": "детей в списке", "from": "откуда", "to": "куда", "date": "дата",
    "created": "создано", "failed": "ошибок", "total": "всего", "skipped": "пропущено",
    "updated": "обновлено", "deleted_records": "удалено записей",
    "ack_records_deleted": "удалено записей ознакомления", "school_name": "название школы",
    "code": "код", "reset": "сброс к стандартному шаблону", "progress": "готово",
    "mark": "отметка", "user": "пользователь", "login_hint": "логин", "ip": "адрес",
    "domain_account": "доменная учётная запись", "groups": "группы AD",
    "employee": "сотрудник", "kind": "тип записи", "category": "категория", "course": "курс",
    "hours": "часов", "provider": "учебное заведение", "document_no": "номер документа",
    "awarded_at": "дата присвоения/окончания", "expires_at": "действует до",
    "recurrence": "повторение",
    # Helpdesk (v1.12)
    "assignee": "исполнитель", "resolution": "что сделано", "reaction_minutes": "реакция, мин",
    "work_minutes": "время в работе, мин", "tickets": "заявки", "count": "количество",
    "chars": "символов", "auto": "автоматически", "contact_phone": "телефон для связи",
    "sla_hours": "норматив реакции, ч", "equipment": "оборудование", "caption": "подпись",
    "pdf": "PDF-копия", "pdf_note": "PDF-копия", "files": "вложений",
    "before": "", "after": "", "task_id": "", "sha256": "", "jti": "", "family": "",
}

MONTHS_RU = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}

ACTION_TITLE_RU = {
    "LOGIN": "Вход в систему", "LOGIN_FAILED": "Неудачная попытка входа", "LOGOUT": "Выход из системы",
    "PASSWORD_CHANGE": "Смена пароля", "TOKEN_REUSE_DETECTED": "Обнаружен повторный токен обновления",
    "SETUP_COMPLETE": "Первичная настройка системы", "SYSTEM_RESTORE": "Восстановление из резервной копии",
    "SYSTEM_SEED": "Инициализация демо-данных", "AUDIT_CLEAR": "Очистка журнала событий",
    "SCHOOL_UPDATE": "Изменено название школы", "ORDER_TYPE_ADD": "Добавлен тип приказов",
    "ORDER_TYPE_DELETE": "Удалён тип приказов", "USER_DEACTIVATE": "Заблокирован пользователь",
    "USER_DELETE": "Удалён пользователь", "USER_RESTORE": "Восстановлен пользователь",
    "USER_BULK_CREATE": "Массовое создание пользователей", "AD_IMPORT": "Импорт пользователей из AD",
    "AD_PROVISION": "Создан пользователь из AD", "AD_ROLE_UPGRADE": "Повышена роль пользователя из AD",
    "AVATAR_UPDATE": "Обновлена фотография профиля", "AVATAR_DELETE": "Удалена фотография профиля",
    "TOTP_SETUP_START": "Начата привязка аутентификатора (ПЭП)", "TOTP_CONFIRMED": "Привязан аутентификатор (ПЭП)",
    "TOTP_REMOVED": "Отвязан аутентификатор (ПЭП)", "TOTP_WORD_SET": "Установлено контрольное слово (ПЭП)",
    "INCOMING_CREATE": "Зарегистрирован входящий документ", "INCOMING_UPDATE": "Изменён входящий документ",
    "INCOMING_DELETE": "Удалён входящий документ", "VSHK_CREATE": "Создан черновик плана ВШК",
    "VSHK_IMPORT": "Импорт плана ВШК", "VSHK_PUBLISH": "Опубликован план ВШК",
    "VSHK_ITEM_CREATE": "Добавлено мероприятие ВШК", "VSHK_ITEM_UPDATE": "Изменено мероприятие ВШК",
    "VSHK_ITEM_DELETE": "Удалено мероприятие ВШК", "VSHK_RESULT": "Подведён итог мероприятия ВШК",
    "VSHK_RESULT_CLEAR": "Очищен итог мероприятия ВШК", "POSITION_CREATE": "Создана должность",
    "POSITION_UPDATE": "Изменена должность", "POSITION_DELETE": "Удалена должность",
    "EQUIPMENT_IMPORT": "Импорт инвентаря", "EQUIPMENT_CREATE": "Добавлена позиция инвентаря",
    "EQUIPMENT_UPDATE": "Изменена позиция инвентаря", "EQUIPMENT_MOVE": "Перемещение инвентаря",
    "EQUIPMENT_DELETE": "Удалена позиция инвентаря", "MOVEMENT_TEMPLATE_UPDATE": "Изменён шаблон приказа о движении",
    "MOVEMENT_CREATE": "Создан приказ о движении", "MOVEMENT_UPDATE": "Изменён приказ о движении",
    "MOVEMENT_DELETE": "Удалён приказ о движении", "PLAN_CREATE": "Создан план работы",
    "PLAN_UPDATE": "Изменён план работы", "PLAN_PUBLISH": "Опубликован план работы",
    "PLAN_ARCHIVE": "План работы перемещён в архив", "PLAN_DELETE": "Удалён черновик плана работы",
    "ORDER_ARCHIVE": "Приказ перемещён в архив",
    "CERT_CREATE": "Добавлена запись об аттестации/КПК", "CERT_UPDATE": "Изменена запись об аттестации/КПК",
    "CERT_DELETE": "Удалена запись об аттестации/КПК", "CERT_IMPORT": "Импорт данных об аттестации/КПК",
    "CERT_FILE": "Приложен скан документа об аттестации/КПК",
    "CERT_FILE_DELETE": "Удалён скан документа об аттестации/КПК",
    # --- Helpdesk (v1.12): заявки сотрудников в техподдержку ---
    "TICKET_CREATE": "Подана заявка в техподдержку", "TICKET_UPDATE": "Уточнена заявка техподдержки",
    "TICKET_TAKE": "Заявка техподдержки взята в работу", "TICKET_REASSIGN": "Заявка передана другому администратору",
    "TICKET_RESOLVE": "Заявка техподдержки выполнена", "TICKET_CLOSE": "Заявка техподдержки закрыта",
    "TICKET_REOPEN": "Заявка возвращена автором на доработку", "TICKET_REJECT": "Заявка техподдержки отклонена",
    "TICKET_CANCEL": "Заявка отозвана", "TICKET_DELETE": "Удалена заявка техподдержки",
    "TICKET_MESSAGE": "Сообщение в заявке техподдержки", "TICKET_MESSAGE_DELETE": "Удалено сообщение в заявке",
    "TICKET_FILE": "Приложен файл к заявке техподдержки", "TICKET_FILE_DELETE": "Удалён файл из заявки",
    "TICKET_SLA_BREACH": "Просрочен норматив реакции по заявкам",
    # --- аварийный сброс аутентификатора Владельцем по контрольному слову (v1.12) ---
    "TOTP_BREAK_GLASS": "Аварийный сброс аутентификатора Владельца (по контрольному слову)",
    # --- документы и инструкции ---
    "NOTE_PDF": "Подготовлена PDF-копия вложения служебной записки",
    "HELP_FILE_DELETE": "Удалён документ инструкции",
}


def _cut(text: str, limit: int = 160) -> str:
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_size(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1024
    return str(value)


def _fmt_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, str) and DATE_RE.match(value.strip()):
        # «2026-10-13» → «13.10.2026» (строки вида list.join — не существует, исправлено)
        return ".".join(value.strip()[:10].split("-")[::-1])
    return None


class _Names:
    """Кэш подстановки uuid → человек/должность для значений diff'ов."""

    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.positions: dict[str, str] = {}

    def resolve_ref(self, ref: str) -> str | None:
        ref = str(ref)
        if ref.startswith("pos:"):
            return self.positions.get(ref[4:])
        return self.users.get(ref)

    def substitute(self, text: str) -> str:
        def repl(m: re.Match[str]) -> str:
            name = self.users.get(m.group(0).lower()) or self.users.get(m.group(0))
            return name or m.group(0)

        return UUID_RE.sub(repl, text)


def _fmt_scalar(key: str, value: Any, names: _Names) -> str:
    if value is None:
        return "пусто"
    if isinstance(value, bool):
        return "да" if value else "нет"
    text = _fmt_date(value)
    if text:
        return text
    if key == "role" or key.endswith("_role"):
        return ROLE_RU.get(str(value), str(value))
    if key == "status":
        return STATUS_RU.get(str(value), str(value))
    if key == "priority":
        return PRIORITY_RU.get(str(value), str(value))
    if key == "source":
        return SOURCE_RU.get(str(value), str(value))
    if key in ("order_type", "type"):
        return ORDER_TYPE_RU.get(str(value), MOVEMENT_TYPE_RU.get(str(value), str(value)))
    if key in ("size",):
        return _fmt_size(value)
    if key == "month":
        try:
            return MONTHS_RU.get(int(value), str(value))
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (list, tuple, set)):
        parts = [_fmt_scalar(key, v, names) for v in list(value)[:8]]
        extra = len(value) - len(parts)
        tail = f" и ещё {extra}" if extra > 0 else ""
        return ", ".join(parts) + tail
    text = names.substitute(_cut(str(value), 200))
    ref = names.resolve_ref(str(value)) if UUID_RE.fullmatch(str(value).strip() or "-") or str(value).startswith("pos:") else None
    return ref or text


def _fmt_value(key: str, value: Any, names: _Names) -> str:
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if key in ("recipients", "assignees", "resp", "recipient_ids", "assignee_ids") or (
            items and all(UUID_RE.fullmatch(str(x)) or str(x).startswith("pos:") for x in items)
        ):
            resolved: list[str] = []
            for ref in items:
                resolved.append(names.resolve_ref(str(ref)) or _cut(str(ref), 40))
            shown = resolved[:6]
            tail = f" и ещё {len(resolved) - len(shown)}" if len(resolved) > len(shown) else ""
            return ", ".join(shown) + tail
        return _fmt_scalar(key, items, names)
    return _fmt_scalar(key, value, names)


def _label(key: str) -> str:
    return FIELD_RU.get(key, key.replace("_", " "))


def _diff_lines(changes: dict[str, Any], names: _Names) -> list[str]:
    """Строки для diff-вида {field: {from,to}} и для плоских изменений."""
    lines: list[str] = []
    for key, value in changes.items():
        if key in ("sha256", "ip", "task_id", "jti", "family", "before", "after"):
            continue
        if isinstance(value, dict) and set(value) <= {"from", "to"}:
            old = value.get("from")
            new = value.get("to")
            if old is not None and new is not None:
                lines.append(f"{_label(key)}: {_fmt_value(key, old, names)} → {_fmt_value(key, new, names)}")
            elif new is not None:
                lines.append(f"{_label(key)}: {_fmt_value(key, new, names)}")
            elif old is not None:
                lines.append(f"{_label(key)}: очищено (было {_fmt_value(key, old, names)})")
            continue
        if value is None:
            continue
        lines.append(f"{_label(key)}: {_fmt_value(key, value, names)}")
    return lines


def _vshk_item_diff(changes: dict[str, Any], names: _Names) -> list[str]:
    before = changes.get("before") or {}
    after = changes.get("after") or {}
    lines: list[str] = []
    for key in ("title", "status", "month", "section", "control_type", "resp", "summary"):
        old, new = before.get(key), after.get(key)
        if str(old) == str(new):
            continue
        if old is not None and new is not None:
            lines.append(f"{_label(key)}: {_fmt_value(key, old, names)} → {_fmt_value(key, new, names)}")
        elif new is not None:
            lines.append(f"{_label(key)}: {_fmt_value(key, new, names)}")
    return lines


# ------------------------------------------------------------------ загрузка сущностей
async def _load_entities(session, rows: list[AuditLog]) -> dict[str, str]:
    """entity_type+id → человекопонятная подпись объекта («приказ № 433»)."""
    labels: dict[str, str] = {}
    by_type: dict[str, set[uuid.UUID]] = {}
    for row in rows:
        if row.entity_type and row.entity_id:
            by_type.setdefault(row.entity_type, set()).add(row.entity_id)

    async def fetch(model, etype, fmt) -> None:
        ids = by_type.get(etype)
        if not ids:
            return
        for obj in (await session.execute(select(model).where(model.id.in_(ids)))).scalars():
            labels[f"{etype}:{obj.id}"] = fmt(obj)

    await fetch(Order, "order", lambda o: f"приказ № {o.number or '—'}")
    await fetch(Task, "task", lambda t: f"задача «{_cut(t.title, 60)}»")
    await fetch(User, "user", lambda u: f"пользователь {u.full_name}")
    await fetch(Position, "position", lambda p: f"должность «{p.title}»")
    await fetch(EquipmentItem, "equipment", lambda e: f"позиция инвентаря «{_cut(e.name, 60)}»")
    await fetch(VshkPlan, "vshk", lambda p: f"план ВШК на {p.year_label} учебный год")
    await fetch(VshkItem, "vshk_item", lambda i: f"мероприятие ВШК «{_cut(i.title, 60)}»")
    await fetch(Plan, "plan", lambda p: f"план работы на неделю {p.week_start:%d.%m.%Y}–{p.week_end:%d.%m.%Y}")
    await fetch(
        IncomingLetter, "incoming",
        lambda i: f"входящий документ № {i.incoming_number} (рег. {i.reg_no}/{i.reg_year})",
    )
    await fetch(
        MovementOrder, "movement",
        lambda m: f"приказ о движении ({MOVEMENT_TYPE_RU.get(m.order_type, m.order_type)}) № {m.order_number}",
    )

    from app.models.certification import Certification

    def _cert_label(c: Certification) -> str:
        what = (c.title or c.category or "запись").strip()
        who = c.user.full_name if c.user else "сотрудник"
        kind = "КПК" if c.kind == "CPK" else "категория"
        return f"{kind} «{_cut(what, 60)}» — {who}"

    await fetch(Certification, "certification", _cert_label)
    return labels


async def _load_names(session, rows: list[AuditLog]) -> _Names:
    """Собирает все uuid из changes и подгружает имена сотрудников и должности."""
    names = _Names()
    user_ids: set[str] = set()
    pos_ids: set[str] = set()
    blob_keys = ("recipients", "assignees", "resp", "recipient_ids", "assignee_ids", "responsible_id")

    def scan(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                scan(v, k)
            return
        if isinstance(value, (list, tuple)):
            for v in value:
                scan(v, key)
            return
        text = str(value)
        if key in blob_keys or key in ("from", "to"):
            for m in UUID_RE.finditer(text):
                user_ids.add(m.group(0).lower())
            if text.startswith("pos:"):
                pos_ids.add(text[4:])
            return
        m = UUID_RE.fullmatch(text.strip())
        if m and key in ("responsible_id", "creator_id", "user"):
            user_ids.add(text.strip().lower())

    for row in rows:
        if row.changes:
            scan(row.changes)

    if user_ids:
        for u in (
            await session.execute(select(User.id, User.full_name).where(User.id.in_([uuid.UUID(i) for i in user_ids])))
        ).all():
            names.users[str(u[0]).lower()] = u[1]
    if pos_ids:
        for p in (
            await session.execute(select(Position.id, Position.title).where(Position.in_([uuid.UUID(i) for i in pos_ids])))
        ).all():
            names.positions[str(p[0])] = p[1]
    return names


# ------------------------------------------------------------------ итоговая фраза
def _summary(row: AuditLog, c: dict[str, Any], entity: str | None, names: _Names) -> str:
    a = row.action

    def num() -> str:
        return str(c.get("number") or c.get("order_number") or "").strip()

    title = _cut(str(c.get("title") or ""), 90)

    if a == "ORDER_CREATE":
        type_ru = ORDER_TYPE_RU.get(str(c.get("type") or ""), "")
        return f"Создан приказ № {num()} «{title}»" + (f" ({type_ru})" if type_ru else "")
    if a == "ORDER_UPDATE":
        return f"Изменён {entity or 'приказ'}"
    if a == "ORDER_PUBLISH":
        return f"{(entity or 'Приказ').capitalize()} опубликован — получателей: {c.get('recipients', 0)}"
    if a == "ORDER_ARCHIVE":
        return f"{(entity or 'Приказ').capitalize()} перемещён в архив"
    if a == "ORDER_DELETE":
        return f"Удалён приказ № {num()} «{title}»"
    if a == "ORDER_FILE_UPLOAD":
        return f"{(entity or 'Приказ').capitalize()}: загружен файл «{_cut(str(c.get('file') or ''), 80)}» ({_fmt_size(c.get('size'))})"
    if a == "ORDER_FILE_DELETE":
        return f"{(entity or 'Приказ').capitalize()}: удалён файл «{_cut(str(c.get('file') or ''), 80)}»"
    if a == "ORDER_RECIPIENTS":
        return f"Изменён круг ознакомления: {(entity or 'приказ')}"
    if a == "ORDER_ACK":
        base = f"Подтверждено ознакомление с приказом № {c.get('order_number') or '—'}"
        method = SIGN_RU.get(str(c.get("sign_method") or ""), None)
        if method:
            base += f" — подпись: {method}"
        return base
    if a in ("TASK_CREATE", "TASK_DELETE"):
        verb = "Создана" if a == "TASK_CREATE" else "Удалена"
        return f"{verb} задача «{title}»"
    if a == "TASK_UPDATE":
        return f"Изменена задача «{title or (entity or '').replace('задача ', '').strip('«»')}»"
    if a == "TASK_STATUS":
        return f"Задача «{_cut(str(c.get('title') or entity or ''), 60)}»: статус «{STATUS_RU.get(str(c.get('from')), str(c.get('from')))}» → «{STATUS_RU.get(str(c.get('to')), str(c.get('to')))}»"
    if a == "TASK_MARK":
        who = c.get("user") or "исполнитель"
        verb = "отметил(а) выполнение" if c.get("mark") == "done" else "снял(а) отметку выполнения"
        return f"{who} {verb} по задаче «{_cut(str(entity or ''), 60)}» (готово {c.get('progress', '')})"
    if a in ("TASK_FILE", "TASK_FILE_DELETE"):
        verb = "прикреплён файл" if a == "TASK_FILE" else "удалён файл"
        return f"К задаче {verb} «{_cut(str(c.get('file') or ''), 80)}»" if a == "TASK_FILE" else f"У задачи удалён файл «{_cut(str(c.get('file') or ''), 80)}»"
    if a == "USER_CREATE":
        role = ROLE_RU.get(str(c.get("role") or ""), str(c.get("role") or ""))
        return f"Создан пользователь {c.get('full_name') or c.get('email') or c.get('login')} — роль: {role}"
    if a == "USER_UPDATE":
        return f"Изменён {entity or 'пользователь'}"
    if a == "USER_DELETE":
        return f"Удалён пользователь {c.get('full_name') or entity or ''}"
    if a == "USER_RESTORE":
        return f"Восстановлен пользователь {c.get('full_name') or entity or ''}"
    if a == "USER_DEACTIVATE":
        return f"Заблокирован {entity or 'пользователь'}"
    if a == "USER_BULK_CREATE":
        return f"Массовое создание пользователей: создано {c.get('created', 0)} из {c.get('total', 0)}, ошибок: {c.get('failed', 0)}"
    if a == "AD_IMPORT":
        return f"Импорт из AD: создано {c.get('created', 0)}, пропущено {c.get('skipped', 0)}, ошибок: {c.get('failed', 0)}"
    if a == "AD_PROVISION":
        return f"Первый вход через AD: создан пользователь {entity or ''}".replace("  ", " ")
    if a == "AD_ROLE_UPGRADE":
        return f"Через AD повышена роль: {ROLE_RU.get(str(c.get('role')), str(c.get('role')))}"
    if a == "INCOMING_CREATE":
        src = SOURCE_RU.get(str(c.get("source") or ""), str(c.get("source") or ""))
        return f"Зарегистрирован входящий № {c.get('incoming_number') or '—'} (рег. {c.get('reg') or '—'})" + (f" — {src}" if src else "")
    if a == "INCOMING_UPDATE":
        return f"Изменён {entity or 'входящий документ'}"
    if a == "INCOMING_DELETE":
        return f"Удалён входящий № {c.get('incoming_number') or '—'} (рег. {c.get('reg') or '—'})"
    if a == "PLAN_CREATE":
        return f"Создан план работы на неделю {c.get('week')}"
    if a == "PLAN_UPDATE":
        return f"Изменён план работы на неделю {c.get('week') or (entity or '').replace('план работы на неделю ', '')}"
    if a == "PLAN_PUBLISH":
        return f"Опубликован план работы на неделю {c.get('week')} — уведомлено: {c.get('notified', 0)}"
    if a == "PLAN_ARCHIVE":
        return f"План работы на неделю {c.get('week')} перемещён в архив"
    if a == "PLAN_DELETE":
        return f"Удалён черновик плана на неделю {c.get('week')}"
    if a == "VSHK_CREATE":
        return f"Создан черновик плана ВШК на {c.get('year')} учебный год"
    if a == "VSHK_IMPORT":
        return f"Импортирован план ВШК на {c.get('year')} учебный год: мероприятий — {c.get('items', 0)}"
    if a == "VSHK_PUBLISH":
        return f"Опубликован план ВШК на {c.get('year')} учебный год: мероприятий — {c.get('items', 0)}, уведомлено — {c.get('notified', 0)}"
    if a == "VSHK_ITEM_CREATE":
        return f"Добавлено мероприятие ВШК ({MONTHS_RU.get(c.get('month'), c.get('month'))}): «{_cut(str(c.get('title') or ''), 80)}»"
    if a == "VSHK_ITEM_UPDATE":
        return f"Изменено мероприятие ВШК «{_cut(str((c.get('after') or {}).get('title') or (c.get('before') or {}).get('title') or ''), 80)}»"
    if a == "VSHK_ITEM_DELETE":
        return f"Удалено мероприятие ВШК «{_cut(str(c.get('title') or ''), 80)}»"
    if a == "VSHK_RESULT":
        return f"Подведён итог мероприятия ВШК «{_cut(str(c.get('title') or ''), 80)}»"
    if a == "VSHK_RESULT_CLEAR":
        return f"Очищен итог мероприятия ВШК «{_cut(str(c.get('title') or ''), 80)}»"
    if a == "POSITION_CREATE":
        return f"Создана должность «{c.get('title')}»"
    if a == "POSITION_UPDATE":
        return f"Изменена должность «{c.get('title')}»"
    if a == "POSITION_DELETE":
        return f"Удалена должность «{c.get('title')}»"
    if a == "ORDER_TYPE_ADD":
        return f"Добавлен тип приказов «{c.get('title')}»"
    if a == "ORDER_TYPE_DELETE":
        return f"Удалён тип приказов «{c.get('title')}»"
    if a == "SCHOOL_UPDATE":
        return f"Изменено название школы: «{c.get('school_name')}»"
    if a == "AUDIT_CLEAR":
        return f"Очищен журнал событий (удалено записей: {c.get('deleted_records', 0)})"
    if a == "SYSTEM_RESTORE":
        return f"База данных восстановлена из файла «{_cut(str(c.get('file') or ''), 80)}»"
    if a == "EQUIPMENT_IMPORT":
        return f"Импорт инвентаря: создано {c.get('created', 0)}, обновлено {c.get('updated', 0)}, ошибок: {c.get('failed', 0)}"
    if a == "EQUIPMENT_CREATE":
        return f"Добавлена позиция инвентаря «{_cut(str(c.get('name') or ''), 70)}» (инв. № {c.get('inventory_number') or '—'})"
    if a == "EQUIPMENT_UPDATE":
        return f"Изменена позиция инвентаря «{_cut(str(c.get('name') or entity or ''), 70)}»"
    if a == "EQUIPMENT_MOVE":
        return f"Перемещён инвентарь «{_cut(str(c.get('name') or ''), 60)}»: {c.get('from')} → {c.get('to')}"
    if a == "EQUIPMENT_DELETE":
        return f"Удалена позиция инвентаря «{_cut(str(c.get('name') or ''), 70)}»"
    if a == "MOVEMENT_CREATE":
        return f"Создан приказ о движении ({_cut(str(c.get('type') or ''), 30)}) № {c.get('number') or '—'}: детей — {c.get('children', 0)}"
    if a == "MOVEMENT_UPDATE":
        return f"Изменён {entity or 'приказ о движении'}"
    if a == "MOVEMENT_DELETE":
        return f"Удалён приказ о движении № {c.get('number') or '—'} ({_cut(str(c.get('student') or ''), 60)})"
    if a == "MOVEMENT_TEMPLATE_UPDATE":
        return "Шаблон приказа о движении сброшен к стандартному" if c.get("reset") else "Изменён шаблон приказа о движении"
    if a == "LOGIN":
        return "Вход в систему"
    if a == "LOGIN_FAILED":
        return f"Неудачная попытка входа ({_cut(str(c.get('login') or c.get('login_hint') or ''), 60)})"
    if a == "LOGOUT":
        return "Выход из системы"
    if a == "PASSWORD_CHANGE":
        return "Смена пароля"
    if a == "AVATAR_UPDATE":
        return "Обновлена фотография профиля"
    if a == "AVATAR_DELETE":
        return "Удалена фотография профиля"
    if a == "TOKEN_REUSE_DETECTED":
        return "Обнаружено повторное использование токена обновления — сессии сброшены"
    if a == "SETUP_COMPLETE":
        return f"Завершена первичная настройка системы (владелец: {c.get('email')})"
    if a == "SYSTEM_SEED":
        return "Инициализация демо-данных"
    return ACTION_TITLE_RU.get(a, a.replace("_", " ").capitalize())


def _lines(row: AuditLog, c: dict[str, Any], names: _Names) -> list[str]:
    a = row.action
    lines: list[str] = []
    if a in ("LOGIN", "LOGIN_FAILED"):
        if a == "LOGIN":
            if c.get("source"):
                lines.append(f"Способ входа: {SOURCE_RU.get(str(c['source']), str(c['source']))}")
            if c.get("role"):
                lines.append(f"Роль: {ROLE_RU.get(str(c['role']), str(c['role']))}")
        else:
            if c.get("login") or c.get("login_hint"):
                lines.append(f"Логин: {c.get('login') or c.get('login_hint')}")
            if c.get("reason"):
                lines.append(f"Причина: {_cut(str(c['reason']), 120)}")
        if row.ip_address:
            lines.append(f"Адрес: {row.ip_address}")
        return lines
    if a == "ORDER_ACK":
        if c.get("sign_method"):
            lines.append(
                "Подпись: " + SIGN_RU.get(str(c["sign_method"]), str(c["sign_method"]))
                + (f" (политика приказа: {c.get('policy')})" if c.get("policy") else "")
            )
        if c.get("comment"):
            lines.append(f"Комментарий: {_cut(str(c['comment']), 160)}")
        if c.get("ip") or row.ip_address:
            lines.append(f"Адрес: {c.get('ip') or row.ip_address}")
        return lines
    if a in ("TOTP_SETUP_START", "TOTP_CONFIRMED", "TOTP_REMOVED", "TOTP_WORD_SET"):
        return lines
    if a == "ORDER_RECIPIENTS":
        refs = c.get("recipients") or []
        if refs:
            lines.append(f"Новый круг ознакомления ({len(refs)}): {_fmt_value('recipients', refs, names)}")
        return lines
    if a in ("ORDER_CREATE", "ORDER_PUBLISH"):
        return []
    if a == "VSHK_ITEM_UPDATE":
        return _vshk_item_diff(c, names)
    if a == "TASK_CREATE":
        if c.get("assignees"):
            lines.append(f"Исполнители: {_fmt_value('assignees', c['assignees'], names)}")
        if c.get("deadline"):
            lines.append(f"Срок: {_fmt_value('deadline', c['deadline'], names)}")
        return lines
    if a == "USER_CREATE":
        parts = []
        if c.get("login"):
            parts.append(f"логин {c['login']}")
        if c.get("email"):
            parts.append(f"e-mail {c['email']}")
        if parts:
            lines.append("Учётная запись: " + ", ".join(parts))
        return lines
    if a in ("AD_PROVISION", "AD_ROLE_UPGRADE"):
        if c.get("groups"):
            lines.append(f"Группы AD: {_fmt_value('groups', c['groups'], names)}")
        return lines
    if a == "INCOMING_DELETE" and c.get("reason"):
        return [f"Причина: {_cut(str(c['reason']), 120)}"]
    if a == "ORDER_DELETE" and c.get("ack_records_deleted"):
        return [f"Удалено записей ознакомления: {c['ack_records_deleted']}"]
    if a == "VSHK_RESULT" and c.get("file"):
        return [f"Файл итога: {_cut(str(c['file']), 100)}"]
    if a == "TASK_FILE" or a == "TASK_FILE_DELETE":
        return []
    # diff-вид и плоские изменения
    lines.extend(_diff_lines(c, names))
    return lines[:12]


async def humanize(session, rows: Iterable[AuditLog]) -> dict[uuid.UUID, dict[str, Any]]:
    """Возвращает {entry.id: {"summary": str, "lines": [str, ...]}}."""
    rows = list(rows)
    names = await _load_names(session, rows)
    entities = await _load_entities(session, rows)
    out: dict[uuid.UUID, dict[str, Any]] = {}
    # «Локдаун»: обезличиваем снимок полей и карту имён ДО рендеринга текста,
    # иначе summary/lines раскрывают ФИО в обход сериализаторов AuditOut.
    from app.services import lockdown

    if lockdown.masking():
        names.users = lockdown.mask_name_map(names.users) or {}
    for row in rows:
        c = lockdown.mask_changes(dict(row.changes or {})) or {}
        entity = entities.get(f"{row.entity_type}:{row.entity_id}") if row.entity_type and row.entity_id else None
        summary = _summary(row, c, entity, names)
        lines = _lines(row, c, names)
        if entity and entity not in summary and row.action in (
            "ORDER_ACK", "ORDER_RECIPIENTS", "TASK_MARK", "TASK_FILE", "TASK_FILE_DELETE",
        ):
            lines.insert(0, f"Объект: {entity}")
        out[row.id] = {"summary": summary or ACTION_TITLE_RU.get(row.action, row.action), "lines": lines}
    return out

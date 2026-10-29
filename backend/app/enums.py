"""Доменные enum'ы — единый источник правды для backend и контракта frontend."""
from __future__ import annotations

import enum


class Role(str, enum.Enum):
    OWNER = "OWNER"        # Владелец школы: полный доступ + управление пользователями/ролями
    ADMIN = "ADMIN"        # Администрация: приказы, задачи, все учителя
    TEACHER = "TEACHER"    # Учитель: свои задачи, ознакомление с приказами


# Числовая иерархия ролей (используется в require_role и в guard'ах «не выше своего уровня»)
ROLE_LEVEL: dict[Role, int] = {Role.TEACHER: 1, Role.ADMIN: 2, Role.OWNER: 3}


class OrderType(str, enum.Enum):
    HR = "HR"                        # По личному составу (приём/увольнение/отпуска)
    STUDENTS = "STUDENTS"            # По обучающимся (зачисление, перевод, отчисление)
    EDUCATION = "EDUCATION"          # По учебной деятельности (УП, расписание, аттестация)
    SAFETY = "SAFETY"                # По охране труда и безопасности
    ADMIN = "ADMIN"                  # Административно-хозяйственные
    OTHER = "OTHER"


class OrderStatus(str, enum.Enum):
    DRAFT = "DRAFT"                  # черновик (виден только OWNER/ADMIN)
    PUBLISHED = "PUBLISHED"          # опубликован — требует ознакомления
    ARCHIVED = "ARCHIVED"            # архив (read-only)


class AckStatus(str, enum.Enum):
    PENDING = "PENDING"              # не ознакомлен
    ACKNOWLEDGED = "ACKNOWLEDGED"    # ознакомлен (подтверждено, есть метаданные)


class TaskStatus(str, enum.Enum):
    NEW = "NEW"                      # Новая
    IN_PROGRESS = "IN_PROGRESS"      # В работе
    DONE = "DONE"                    # Выполнена
    # OVERDUE («Просрочена») — ВЫЧИСЛЯЕМОЕ состояние: status != DONE && deadline < now.
    # Хранить его в БД нельзя: оно устаревает само по себе и требует cron'а.
    # См. app/services/tasks.py::is_overdue и effective_status().


OVERDUE = "OVERDUE"                  # виртуальный статус для API/UI


class PlanStatus(str, enum.Enum):
    DRAFT = "DRAFT"                  # черновик недели (виден только OWNER/ADMIN)
    PUBLISHED = "PUBLISHED"          # опубликован — видят все, ответственные уведомлены
    ARCHIVED = "ARCHIVED"            # архив прошлых недель (read-only)


class VshkStatus(str, enum.Enum):
    """Статус мероприятия внутришкольного контроля (ВШК)."""

    PLANNED = "PLANNED"              # запланировано на месяц
    IN_WORK = "IN_WORK"              # идёт проведение
    DONE = "DONE"                    # выполнено, итог подведён


# --- ПЭП через аутентификатор (TOTP): уровни подписи и политики приказов ---

class SignaturePolicy(str, enum.Enum):
    """Требуемый уровень подписи ознакомления с приказом."""

    NONE = "NONE"            # без подписи: фиксация времени с записью в аудит
    CLICK = "CLICK"          # клик «Ознакомлен» (текущее поведение системы)
    TOTP = "TOTP"            # одноразовый код из приложения-аутентификатора
    CODE_WORD = "CODE_WORD"  # код TOTP + контрольное слово (максимальный уровень)


# Автоправило «тип приказа → политика»: тип STUDENTS (движение обучающихся) — всегда усиленная.
ORDER_TYPE_SIGNATURE_POLICY: dict[str, str] = {
    "STUDENTS": SignaturePolicy.TOTP.value,
}


class AckSignMethod(str, enum.Enum):
    """Способ подписи, фактически зафиксированный при ознакомлении."""

    NONE = "NONE"                    # без подписи (политика NONE, зафиксировано в аудите)
    CLICK = "CLICK"                  # простой клик «Ознакомлен»
    TOTP = "TOTP"                    # подтверждение одноразовым кодом
    CODE_WORD = "CODE_WORD"          # код TOTP + контрольное слово


class NoteStatus(str, enum.Enum):
    """State machine служебной записки (см. NOTE_TRANSITIONS)."""

    DRAFT = "DRAFT"                      # черновик (виден только автору)
    ON_APPROVAL = "ON_APPROVAL"          # на согласовании
    APPROVED = "APPROVED"                # утверждена (подпись кодом) → создана задача исполнителю
    REJECTED = "REJECTED"                # отклонена (возврат автору с комментарием)
    COMPLETED = "COMPLETED"              # выполнена (закрыта вместе с задачей)


NOTE_TRANSITIONS: dict[str, set[str]] = {
    NoteStatus.DRAFT.value: {NoteStatus.ON_APPROVAL.value},
    NoteStatus.ON_APPROVAL.value: {NoteStatus.APPROVED.value, NoteStatus.REJECTED.value},
    NoteStatus.REJECTED.value: {NoteStatus.ON_APPROVAL.value},     # автор доработал — снова на согласование
    NoteStatus.APPROVED.value: {NoteStatus.COMPLETED.value},       # как правило, выводится из задачи
    NoteStatus.COMPLETED.value: set(),
}


class HelpVisibility(str, enum.Enum):
    """Кто видит статью справки / инструкцию (FAQ).

    Служебные инструкции часто адресованы не всем: приказы по кадрам, регламенты
    согласования, правила работы с персональными данными. Значение задаёт автор
    статьи и оно же ограничивает лист ознакомления (см. app/services/help.py).
    """

    ALL = "ALL"          # всем авторизованным сотрудникам (прежнее поведение)
    ADMIN = "ADMIN"      # только администрации: ADMIN и OWNER
    OWNER = "OWNER"      # только Владельцу


# Минимальный уровень роли, при котором статья видна (см. ROLE_LEVEL)
HELP_VISIBILITY_MIN_ROLE: dict[HelpVisibility, int] = {
    HelpVisibility.ALL: ROLE_LEVEL[Role.TEACHER],
    HelpVisibility.ADMIN: ROLE_LEVEL[Role.ADMIN],
    HelpVisibility.OWNER: ROLE_LEVEL[Role.OWNER],
}


class SubstitutionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"        # замещение действует (или запланировано)
    CONFIRMED = "CONFIRMED"  # замещающий подтвердил принятие
    ENDED = "ENDED"          # завершено (вручную или по истечении периода)
    CANCELLED = "CANCELLED"  # отменено инициатором


# Разрешённые переходы статусов задачи (state machine)
TASK_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.NEW.value: {TaskStatus.IN_PROGRESS.value, TaskStatus.DONE.value},
    TaskStatus.IN_PROGRESS.value: {TaskStatus.DONE.value, TaskStatus.NEW.value},
    TaskStatus.DONE.value: {TaskStatus.IN_PROGRESS.value},   # откат, если принято неверно
}


# ---------------------------------------------------------------------------
# Helpdesk: заявки сотрудников в техническую поддержку школы (v1.12)
# ---------------------------------------------------------------------------

class TicketStatus(str, enum.Enum):
    """Жизненный цикл заявки (state machine — TICKET_TRANSITIONS).

    NEW         — заявка подана, ждёт системного администратора;
    IN_PROGRESS — администратор взял в работу (assignee_id заполнен);
    RESOLVED    — работа выполнена, ждём подтверждения автора;
    CLOSED      — автор подтвердил (или сработало автозакрытие);
    REJECTED    — отклонена администратором с комментарием (не наш профиль);
    CANCELLED   — отозвана автором.
    """

    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# Открытые статусы: по ним считаются «заявки в работе» и SLA-просрочка.
TICKET_OPEN_STATUSES: tuple[str, ...] = (
    TicketStatus.NEW.value,
    TicketStatus.IN_PROGRESS.value,
)

# Заявка ещё «живая»: автор может её отозвать, администратор — взять в работу.
TICKET_ACTIVE_STATUSES: tuple[str, ...] = (
    TicketStatus.NEW.value,
    TicketStatus.IN_PROGRESS.value,
    TicketStatus.RESOLVED.value,
)

TICKET_TRANSITIONS: dict[str, set[str]] = {
    TicketStatus.NEW.value: {
        TicketStatus.IN_PROGRESS.value,   # взяли в работу
        TicketStatus.REJECTED.value,      # отклонили
        TicketStatus.CANCELLED.value,     # автор отозвал
    },
    TicketStatus.IN_PROGRESS.value: {
        TicketStatus.RESOLVED.value,      # выполнили
        TicketStatus.NEW.value,           # вернули в очередь (сменился администратор)
        TicketStatus.REJECTED.value,
        TicketStatus.CANCELLED.value,
    },
    TicketStatus.RESOLVED.value: {
        TicketStatus.CLOSED.value,        # автор подтвердил
        TicketStatus.IN_PROGRESS.value,   # автор вернул на доработку
    },
    # терминальные: из них заявка не выходит, исправление — новая заявка
    TicketStatus.CLOSED.value: set(),
    TicketStatus.REJECTED.value: set(),
    TicketStatus.CANCELLED.value: set(),
}


class TicketPriority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


#: Норматив РЕАКЦИИ (взятия в работу) в рабочих часах. Это не «срок починки»:
#: починка зависит от запчастей, а вот отреагировать администратор обязан.
TICKET_SLA_HOURS: dict[str, int] = {
    TicketPriority.URGENT.value: 2,
    TicketPriority.HIGH.value: 4,
    TicketPriority.NORMAL.value: 8,
    TicketPriority.LOW.value: 24,
}

#: Через сколько дней после RESOLVED заявка закрывается автоматически,
#: если автор так и не подтвердил и не вернул её на доработку (housekeeping).
TICKET_AUTOCLOSE_DAYS = 5


class TicketCategory(str, enum.Enum):
    """Что сломалось. Значения соответствуют реальной школьной технике:
    именно эти формулировки учитель выбирает из списка, а не придумывает сам."""

    PRINTER = "PRINTER"            # принтер / МФУ / сканер
    COMPUTER = "COMPUTER"          # компьютер / ноутбук не включается, тормозит
    NETWORK = "NETWORK"            # нет интернета / Wi-Fi
    BOARD = "BOARD"                # интерактивная доска / панель
    PROJECTOR = "PROJECTOR"        # проектор, экран
    DOC_CAMERA = "DOC_CAMERA"      # документ-камера
    AUDIO = "AUDIO"                # звук, микрофоны, колонки
    SOFTWARE = "SOFTWARE"          # программы, ЭЦК, электронный журнал, антивирус
    ACCOUNT = "ACCOUNT"            # доступы, пароли, почта, SchoolHub
    PHONE = "PHONE"                # телефония
    OTHER = "OTHER"                # прочее


class TicketFileKind(str, enum.Enum):
    """Роль вложения в заявке: фото поломки или документ, добавленный администратором."""

    AUTHOR = "AUTHOR"      # приложил автор заявки
    STAFF = "STAFF"        # приложил системный администратор (акт, счёт, скан)


class TicketMessageKind(str, enum.Enum):
    MESSAGE = "MESSAGE"    # реплика участника
    SYSTEM = "SYSTEM"      # служебная отметка (смена статуса, назначение)


class PdfStatus(str, enum.Enum):
    """Состояние автоматической PDF-копии загруженного документа (v1.12).

    NA      — формат не конвертируется (архив и т.п.), открывается как есть;
    PENDING — очередь на конвертацию ещё не отработала;
    READY   — PDF-копия готова и лежит в pdf_path;
    FAILED  — конвертер не справился (таймаут/нет LibreOffice), доступен оригинал.
    """

    NA = "NA"
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, field_serializer

from app.schemas.order import OrderListOut
from app.schemas.plan import PlanOut
from app.schemas.task import TaskOut


class OrderStats(BaseModel):
    total: int = 0
    published: int = 0
    drafts: int = 0
    pending_ack: int = 0        # сколько приказов ждёт МОЕГО ознакомления (учитель) / всего неподтверждённых (админ)


class TaskStats(BaseModel):
    total: int = 0
    new: int = 0
    in_progress: int = 0
    done: int = 0
    overdue: int = 0


class UserStats(BaseModel):
    total: int = 0
    teachers: int = 0
    admins: int = 0
    active: int = 0


class DashboardSummary(BaseModel):
    scope: str                  # "personal" (учитель) | "global" (админ/владелец)
    orders: OrderStats
    tasks: TaskStats
    users: UserStats | None = None


class OverdueAck(BaseModel):
    order_id: str
    number: str
    title: str
    user_id: str
    user_name: str
    deadline: str
    days_overdue: int

    @field_serializer("user_name")
    def _mask_user(self, v: str) -> str:
        from app.services import lockdown

        return lockdown.mask_employee(self.user_id) if lockdown.masking() else v


class MyPlanEvent(BaseModel):
    """Пункт плана текущей недели, где пользователь — ответственный (виджет дашборда)."""

    kind: str                        # duty | event | week | month
    date: str | None = None          # ISO-дата (для мероприятий конкретного дня)
    weekday: str | None = None       # «понедельник» и т.п.
    time: str | None = None
    place: str | None = None
    title: str


class MyVshkItem(BaseModel):
    """Мероприятие ВШК текущего месяца, где сотрудник — ответственный."""

    id: str
    plan_id: str
    month: int
    section: str = ""
    title: str
    control_type: str = ""
    summary: str = ""
    status: str                      # PLANNED | IN_WORK | DONE
    result_text: str | None = None


class BirthdayOut(BaseModel):
    """Ближайший день рождения сотрудника (виджет дашборда для администрации)."""

    user_id: uuid.UUID
    full_name: str
    birth_date: date
    days_until: int
    turns_age: int

    @field_serializer("full_name")
    def _mask_name(self, v: str) -> str:
        from app.services import lockdown

        return lockdown.mask_employee(str(self.user_id)) if lockdown.masking() else v


class DashboardOut(BaseModel):
    summary: DashboardSummary
    current_plan: PlanOut | None = None      # план работы текущей недели (виджет на дашборде)
    my_plan_events: list[MyPlanEvent] = []   # мои пункты/мероприятия из плана этой недели
    my_vshk: list[MyVshkItem] = []           # мои мероприятия ВШК текущего месяца
    pending_acks: list[OrderListOut] = []      # приказы, требующие ознакомления (для учителя)
    my_tasks: list[TaskOut] = []               # ближайшие задачи (для учителя) / незавершённые по школе (для админа)
    recent_orders: list[OrderListOut] = []
    recent_tasks: list[TaskOut] = []
    overdue_acks: list[OverdueAck] = []        # просроченные ознакомления (для администрации)
    birthdays: list[BirthdayOut] = []          # ближайшие дни рождения (для администрации)


class MonthTypeCount(BaseModel):
    type: str
    count: int


class MonthSummaryOut(BaseModel):
    """Отчёт «Итоги месяца» для администрации (кнопка на дашборде)."""

    month: str                              # «2026-10»
    orders_published: int                   # приказов опубликовано за месяц
    orders_by_type: list[MonthTypeCount] = []   # разбивка по типам приказов
    orders_ack_complete: int                # из них полностью ознакомлены (на сейчас)
    tasks_created: int                      # задач создано за месяц
    tasks_done: int                         # задач выполнено за месяц (по истории статусов)
    tasks_overdue: int                      # дедлайн в месяце, не выполнены на сейчас
    incoming_count: int                     # писем зарегистрировано в журнале за месяц
    movement_count: int                     # заявлений на движение создано за месяц

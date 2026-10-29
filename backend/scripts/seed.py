"""Seed демо-данных SchoolHub.

Запуск:
    python -m scripts.seed            # идемпотентно: если данные есть — пропуск
    python -m scripts.seed --force    # пересоздать демо-данные (ВНИМАНИЕ: очистка таблиц)

Учётные записи (пароль у всех демо-пользователей: `Demo1234!`):
    owner@schoolhub.ru    — Владелец
    admin@schoolhub.ru    — Администрация (завуч)
    tech@schoolhub.ru     — Администрация (зам. по АХЧ)
    ivanov@, petrova@, smirnov@, kuznecova@, malysheva@, avdeeva@,
    sherina@, sultanova@, fedotova@  — Учителя и специалисты
"""
from __future__ import annotations

import asyncio
import io
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

from sqlalchemy import delete, func, select

from app.config import settings
from app.database import SessionLocal, init_db
from app.enums import AckStatus, OrderStatus, OrderType, PlanStatus, Role, TaskStatus
from app.models.audit import AuditLog
from app.models.notification import Notification
from app.models.order import Order, OrderAcknowledgement, OrderFile
from app.models.plan import Plan
from app.models.task import Task, TaskAssignee, TaskComment, TaskFile, TaskStatusHistory
from app.models.user import User
from app.security import hash_password
from app.services.storage import storage
from app.types import enum_value
from scripts.demo_docs import DemoDoc, build_docx, build_pdf

DEMO_PASSWORD = "Demo1234!"
TODAY = date.today()
YEAR = TODAY.year
MONDAY = TODAY - timedelta(days=TODAY.weekday())   # понедельник текущей недели

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ------------------------------------------------------------------- участники
USERS: list[dict] = [
    dict(email="owner@schoolhub.ru", full_name="Соколова Ирина Аркадьевна", role=Role.OWNER,
         position="Директор школы (учредитель)", phone="+7 495 100-00-01"),
    dict(email="admin@schoolhub.ru", full_name="Морозова Ольга Петровна", role=Role.ADMIN,
         position="Заместитель директора по УВР", phone="+7 495 100-00-02"),
    dict(email="tech@schoolhub.ru", full_name="Гаврилов Сергей Николаевич", role=Role.ADMIN,
         position="Заместитель директора по АХЧ", phone="+7 495 100-00-03"),
    dict(email="ivanov@schoolhub.ru", full_name="Иванов Пётр Сергеевич", role=Role.TEACHER,
         position="Учитель математики", phone="+7 495 100-00-11"),
    dict(email="petrova@schoolhub.ru", full_name="Петрова Анна Викторовна", role=Role.TEACHER,
         position="Учитель русского языка и литературы", phone="+7 495 100-00-12"),
    dict(email="smirnov@schoolhub.ru", full_name="Смирнов Алексей Игоревич", role=Role.TEACHER,
         position="Учитель физической культуры", phone="+7 495 100-00-13"),
    dict(email="kuznecova@schoolhub.ru", full_name="Кузнецова Мария Львовна", role=Role.TEACHER,
         position="Учитель начальных классов", phone="+7 495 100-00-14"),
    dict(email="malysheva@schoolhub.ru", full_name="Малышева Марина Ивановна", role=Role.TEACHER,
         position="Педагог-психолог", phone="+7 495 100-00-15"),
    dict(email="avdeeva@schoolhub.ru", full_name="Авдеева Елена Юрьевна", role=Role.TEACHER,
         position="Педагог-организатор", phone="+7 495 100-00-16"),
    dict(email="sherina@schoolhub.ru", full_name="Шерина Светлана Викторовна", role=Role.TEACHER,
         position="Учитель информатики", phone="+7 495 100-00-17"),
    dict(email="sultanova@schoolhub.ru", full_name="Султанова Альбина Рустамовна", role=Role.TEACHER,
         position="Учитель музыки, руководитель театра «Этюд»", phone="+7 495 100-00-18"),
    dict(email="fedotova@schoolhub.ru", full_name="Федотова Екатерина Викторовна", role=Role.TEACHER,
         position="Учитель биологии, кл. рук. 9 «Б»", phone="+7 495 100-00-19"),
]

ORDERS: list[dict] = [
    dict(
        number="112-к", order_type=OrderType.HR, status=OrderStatus.PUBLISHED,
        issued_on=TODAY - timedelta(days=9), ack_deadline=TODAY + timedelta(days=5),
        title="О приёме на работу и распределении учебной нагрузки на 2-е полугодие",
        description="Приём 2 педагогов, перераспределение часов, доплаты за классное руководство.",
        preamble=(
            "В целях обеспечения учебно-воспитательного процесса и выполнения трудового законодательства "
            "Российской Федерации, на основании статей 22 и 57 Трудового кодекса РФ,"
        ),
        items=[
            "Принять Кузнецову Марию Львовну на должность учителя начальных классов на условиях полного рабочего времени с окладом согласно штатному расписанию.",
            "Установить учебную нагрузку на 2-е полугодие согласно приложению №1 к настоящему приказу.",
            "Установить доплату за классное руководство в размере 5000 рублей ежемесячно учителям, за которыми закреплены классы.",
            "Отделу кадров оформить дополнительные соглашения к трудовым договорам в срок до 20 числа текущего месяца.",
        ],
        controls=["4. Контроль за исполнением настоящего приказа оставляю за собой."],
        fmt="pdf",
        acked=["ivanov@schoolhub.ru", "petrova@schoolhub.ru"],
    ),
    dict(
        number="87", order_type=OrderType.SAFETY, status=OrderStatus.PUBLISHED,
        issued_on=TODAY - timedelta(days=4), ack_deadline=TODAY + timedelta(days=2),
        title="Об утверждении инструкций по охране труда и пожарной безопасности",
        description="Обязателен к ознакомлению всеми сотрудниками. Проверка знаний — до конца недели.",
        preamble=(
            "В соответствии со статьёй 214 Трудового кодекса РФ, Правилами противопожарного режима "
            "в Российской Федерации и в целях обеспечения безопасных условий труда работников,"
        ),
        items=[
            "Утвердить и ввести в действие инструкции по охране труда согласно перечню (приложение №1).",
            "Всем педагогическим работникам пройти внеплановый инструктаж по пожарной безопасности.",
            "Назначить ответственным за пожарную безопасность в кабинетах — заведующих кабинетами.",
            "Провести проверку знаний требований охраны труда в срок до конца текущей недели.",
        ],
        controls=["5. Контроль за исполнением приказа возложить на заместителя директора по АХЧ Гаврилова С. Н."],
        fmt="pdf",
        acked=["smirnov@schoolhub.ru", "avdeeva@schoolhub.ru"],
    ),
    dict(
        number="45", order_type=OrderType.EDUCATION, status=OrderStatus.PUBLISHED,
        issued_on=TODAY - timedelta(days=2), ack_deadline=TODAY + timedelta(days=10),
        title="О подготовке и проведении Всероссийских проверочных работ (ВПР)",
        description="График ВПР, ответственные, порядок заполнения ведомостей. Круг ознакомления — только организаторы ВПР.",
        preamble=(
            "На основании письма Федеральной службы по надзору в сфере образования и науки "
            "об организации проведения Всероссийских проверочных работ,"
        ),
        items=[
            "Утвердить график проведения ВПР согласно приложению №1.",
            "Назначить организаторов в аудиториях из числа педагогических работников.",
            "Учителям-предметникам обеспечить повторение программного материала по соответствующим предметам.",
            "Результаты внести в электронный журнал в течение трёх рабочих дней после проведения работы.",
        ],
        controls=["5. Контроль за исполнением приказа возложить на заместителя директора по УВР Морозову О. П."],
        fmt="docx",
        # выборочный круг ознакомления (демонстрация require_all=False)
        recipients=["ivanov@schoolhub.ru", "petrova@schoolhub.ru", "kuznecova@schoolhub.ru", "malysheva@schoolhub.ru"],
        acked=["kuznecova@schoolhub.ru"],
    ),
    dict(
        number="19", order_type=OrderType.ADMIN, status=OrderStatus.ARCHIVED,
        issued_on=date(YEAR, 1, 15),
        title="О режиме работы школы в период зимних каникул",
        description="Архивный документ: ознакомление завершено, приказ закрыт.",
        preamble="В целях организации работы школы в период зимних каникул и обеспечения безопасности обучающихся,",
        items=[
            "Установить режим работы школы на период каникул: 09:00 — 16:00.",
            "Дежурство администрации организовать по графику (приложение №1).",
            "Провести ревизию систем отопления и электроснабжения до начала третьей четверти.",
        ],
        controls=["4. Контроль за исполнением приказа оставляю за собой."],
        fmt="pdf",
        acked="all",
    ),
    dict(
        number="120-к", order_type=OrderType.HR, status=OrderStatus.DRAFT,
        issued_on=TODAY,
        title="О предоставлении ежегодного оплачиваемого отпуска (черновик)",
        description="Черновик — виден только Владельцу и Администрации, готовится к публикации.",
        preamble="В соответствии со статьями 114 и 123 Трудового кодекса РФ и утверждённым графиком отпусков,",
        items=[
            "Предоставить ежегодный основной оплачиваемый отпуск продолжительностью 28 календарных дней.",
            "Бухгалтерии произвести расчёт отпускных не позднее чем за три дня до начала отпуска.",
        ],
        controls=["3. Контроль за исполнением приказа оставляю за собой."],
        fmt="pdf",
        acked=[],
    ),
]

# assignees — несколько исполнителей; marks — кто уже отметил «выполнено»; files — вложения
TASKS: list[dict] = [
    dict(title="Заполнить ведомости ВПР по математике (7 «А», 7 «Б»)",
         assignees=["ivanov@schoolhub.ru", "sherina@schoolhub.ru"], marks=[],
         creator="admin@schoolhub.ru", status=TaskStatus.IN_PROGRESS, priority="HIGH",
         deadline=datetime.now(timezone.utc) + timedelta(days=2, hours=4),
         description="Внести результаты в электронный журнал и подписать бумажные ведомости у завуча.",
         files=[("ведомость_7А_черновик.xlsx", XLSX_MIME, "Черновик ведомости ВПР, 7 «А»")]),
    dict(title="Подготовить кабинет №12 к проверке пожарной инспекции",
         assignees=["ivanov@schoolhub.ru", "smirnov@schoolhub.ru"], marks=[],
         creator="tech@schoolhub.ru", status=TaskStatus.NEW, priority="URGENT",
         deadline=datetime.now(timezone.utc) - timedelta(days=1, hours=6),
         description="Проверить огнетушитель, план эвакуации, состояние розеток и проводов."),
    dict(title="Сдать отчёт по успеваемости за 1-е полугодие",
         assignees=["ivanov@schoolhub.ru"], marks=["ivanov@schoolhub.ru"],
         creator="admin@schoolhub.ru", status=TaskStatus.DONE, priority="NORMAL",
         deadline=datetime.now(timezone.utc) - timedelta(days=12),
         completed=datetime.now(timezone.utc) - timedelta(days=14)),
    dict(title="Проверка сочинений 11 «Б» + выставление оценок",
         assignees=["petrova@schoolhub.ru"], marks=[],
         creator="admin@schoolhub.ru", status=TaskStatus.NEW, priority="HIGH",
         deadline=datetime.now(timezone.utc) + timedelta(days=1, hours=12),
         description="Оценки внести в журнал до 20:00, скан ведомости приложить вложением."),
    dict(title="Оформить стенд «Гордость школы» к 20 числу",
         assignees=["kuznecova@schoolhub.ru", "avdeeva@schoolhub.ru"], marks=["kuznecova@schoolhub.ru"],
         creator="tech@schoolhub.ru", status=TaskStatus.IN_PROGRESS, priority="NORMAL",
         deadline=datetime.now(timezone.utc) + timedelta(days=6),
         description="Обновить фотографии и список достижений за четверть."),
    dict(title="Провести инструктаж по ТБ на уроках физкультуры",
         assignees=["smirnov@schoolhub.ru"], marks=[],
         creator="tech@schoolhub.ru", status=TaskStatus.NEW, priority="URGENT",
         deadline=datetime.now(timezone.utc) - timedelta(hours=20),
         description="Отметить в журнале инструктажей, лист ознакомления прикрепить в SchoolHub."),
    dict(title="Подготовить методические материалы к педсовету",
         assignees=["petrova@schoolhub.ru", "malysheva@schoolhub.ru"], marks=["petrova@schoolhub.ru"],
         creator="owner@schoolhub.ru", status=TaskStatus.IN_PROGRESS, priority="NORMAL",
         deadline=datetime.now(timezone.utc) + timedelta(days=4),
         description="Раздел «Адаптация пятиклассников» — справка психолога и выводы по итогам четверти.",
         files=[("справка_адаптация_v1.pdf", PDF_MIME, "Справка психолога об адаптации 5-х классов")]),
    dict(title="Инвентаризация спортивного инвентаря",
         assignees=["smirnov@schoolhub.ru", "tech@schoolhub.ru"],
         marks=["smirnov@schoolhub.ru", "tech@schoolhub.ru"],
         creator="tech@schoolhub.ru", status=TaskStatus.DONE, priority="LOW",
         deadline=datetime.now(timezone.utc) - timedelta(days=5),
         completed=datetime.now(timezone.utc) - timedelta(days=6)),
    # --- повторяющиеся задачи (демо): дедлайна нет, срок — период повторения ---
    dict(title="Дежурство: проверить температурный режим в кабинетах",
         assignees=["tech@schoolhub.ru"], marks=[],
         creator="admin@schoolhub.ru", status=TaskStatus.NEW, priority="NORMAL",
         recurrence="DAILY",
         description="Повторяющаяся задача: каждый день отмечайте выполнение — к утру она снова станет новой."),
    dict(title="Еженедельная сверка журнала входящих писем",
         assignees=["ivanov@schoolhub.ru"], marks=[],
         creator="owner@schoolhub.ru", status=TaskStatus.NEW, priority="NORMAL",
         recurrence="WEEKLY", recurrence_weekdays=[5],
         description="Каждую пятницу: сверить электронные и бумажные регистрации."),
    dict(title="Ежемесячная проверка огнетушителей",
         assignees=["smirnov@schoolhub.ru"], marks=[],
         creator="tech@schoolhub.ru", status=TaskStatus.NEW, priority="HIGH",
         recurrence="MONTHLY", recurrence_day=1,
         description="Первого числа каждого месяца: осмотр, отметка в журнале."),
]


# --------------------------------------------------- план работы (текущая неделя)
def _plan_content(uid: dict[str, str]) -> dict:
    """Content текущего плана — зеркалит Word-макет школы (ответственные — UUID-строки)."""
    return {
        "duty": {"klass": "9 «Б»", "teacher_id": uid["fedotova@schoolhub.ru"]},
        "notes": ["В столовой дежурят 7-ые классы."],
        "month_items": [
            {"text": "Контроль адаптации учеников 1-х, 5-х и 10-х классов (посещение уроков, диагностика).",
             "place": None, "resp": [uid["admin@schoolhub.ru"], uid["malysheva@schoolhub.ru"]]},
            {"text": "Проверка организации подготовки обучающихся к ГИА (посещение уроков в 9, 11-х классах).",
             "place": None, "resp": [uid["admin@schoolhub.ru"]]},
            {"text": "Старт профориентационного проекта «АтомПрофиСтарт».",
             "place": None, "resp": [uid["sherina@schoolhub.ru"], uid["kuznecova@schoolhub.ru"]]},
        ],
        "week_items": [
            {"text": "Школьный этап Всероссийской олимпиады школьников (ВсОШ) — по отдельным приказам.",
             "place": "Кабинеты-предметные",
             "resp": [uid["admin@schoolhub.ru"], uid["ivanov@schoolhub.ru"], uid["petrova@schoolhub.ru"]]},
            {"text": "Социально-психологическое тестирование (СПТ) обучающихся 7–11-х классов.",
             "place": "Классные кабинеты",
             "resp": [uid["admin@schoolhub.ru"], uid["malysheva@schoolhub.ru"]]},
            {"text": "Подготовка к фестивалю «Народов много — Родина одна!», посвящённому Дню народного единства.",
             "place": "Актовый зал",
             "resp": [uid["avdeeva@schoolhub.ru"], uid["sultanova@schoolhub.ru"]]},
            {"text": "Подготовка документов к семейному конкурсу мастерства «Папа может», посвящённому Дню отца.",
             "place": None,
             "resp": [uid["avdeeva@schoolhub.ru"], uid["smirnov@schoolhub.ru"]]},
        ],
        "days": [
            {"date": str(MONDAY + timedelta(days=0)), "events": [
                {"title": "«Разговоры о важном».", "time": "08.00", "place": "Кл. каб.",
                 "resp": [uid["kuznecova@schoolhub.ru"], uid["petrova@schoolhub.ru"]]},
                {"title": "Совещание с кл. рук. 7–11-х классов по вопросу организации проведения СПТ.",
                 "time": "09.10", "place": "Каб. 405",
                 "resp": [uid["admin@schoolhub.ru"], uid["malysheva@schoolhub.ru"]]},
                {"title": "ВсОШ по математике (5–6 классы, теоретический тур).", "time": "По отд. приказу",
                 "place": "Каб. 301", "resp": [uid["ivanov@schoolhub.ru"]]},
                {"title": "Совещание при директоре.", "time": "15.00", "place": "ЦПД",
                 "resp": [uid["owner@schoolhub.ru"], uid["admin@schoolhub.ru"], uid["tech@schoolhub.ru"]]},
            ]},
            {"date": str(MONDAY + timedelta(days=1)), "events": [
                {"title": "ПРофРазговор для 4-х кл. «Инженер: укротитель металла и энергии».",
                 "time": "12.00", "place": "Акт. зал",
                 "resp": [uid["kuznecova@schoolhub.ru"], uid["sherina@schoolhub.ru"]]},
                {"title": "ВсОШ по математике (5–6 классы, практический тур).", "time": "По отд. приказу",
                 "place": "Каб. 301", "resp": [uid["ivanov@schoolhub.ru"]]},
                {"title": "Проведение СПТ для 7–11 кл.", "time": "По расписанию", "place": "Кл. каб.",
                 "resp": [uid["admin@schoolhub.ru"], uid["malysheva@schoolhub.ru"]]},
            ]},
            {"date": str(MONDAY + timedelta(days=2)), "events": [
                {"title": "Собрание школьного СМИ.", "time": "10.05", "place": "Акт. зал",
                 "resp": [uid["avdeeva@schoolhub.ru"], uid["petrova@schoolhub.ru"]]},
                {"title": "ВсОШ по русскому языку (7–11 классы).", "time": "По отд. приказу",
                 "place": "Каб. 210", "resp": [uid["petrova@schoolhub.ru"]]},
                {"title": "МИНИКЭС-БАСКЕТ, школьный этап (4 классы).", "time": "14.00",
                 "place": "Спортивный зал", "resp": [uid["smirnov@schoolhub.ru"]]},
            ]},
            {"date": str(MONDAY + timedelta(days=3)), "events": [
                {"title": "«Россия — мои горизонты».", "time": "08.00", "place": "Кл. каб.",
                 "resp": [uid["petrova@schoolhub.ru"], uid["kuznecova@schoolhub.ru"]]},
                {"title": "Городское методическое объединение учителей информатики.", "time": "10.00",
                 "place": "СШ №2", "resp": [uid["sherina@schoolhub.ru"]]},
                {"title": "АтомПрофиКвиз «Инженерный ринг» для 4-х кл.", "time": "По графику",
                 "place": "IT-куб", "resp": [uid["sherina@schoolhub.ru"], uid["kuznecova@schoolhub.ru"]]},
            ]},
            {"date": str(MONDAY + timedelta(days=4)), "events": [
                {"title": "ВсОШ по физике (9–11 классы).", "time": "По отд. приказу", "place": "Каб. 305",
                 "resp": [uid["admin@schoolhub.ru"]]},
                {"title": "Интеллектуальная игра «Что? Где? Когда?» — команда учителей против команды родителей.",
                 "time": "По отд. приказу", "place": "Акт. зал",
                 "resp": [uid["avdeeva@schoolhub.ru"], uid["sultanova@schoolhub.ru"]]},
            ]},
            {"date": str(MONDAY + timedelta(days=5)), "events": [
                {"title": "Футбольный турнир для пап и сыновей (5–7 классы).", "time": "08.30–13.00",
                 "place": "Спортзал / стадион (по погоде)",
                 "resp": [uid["smirnov@schoolhub.ru"], uid["sultanova@schoolhub.ru"]]},
            ]},
        ],
    }


# --------------------------------------------------------------------- утилиты
async def _save_file(data: bytes, filename: str, mime: str, allowed: list[str] | None = None):
    """Пишем байты через то же хранилище, что и боевой код (content-addressed)."""
    return await storage.save(io.BytesIO(data), filename, mime, allowed_mimes=allowed)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _xlsx_stub(title: str) -> bytes:
    """Минимальный корректный XLSX (OOXML-пакет) — проходит проверку сигнатуры."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook><sheets><sheet name="{title[:20]}"/></sheets></workbook>',
        )
    return buf.getvalue()


async def _wipe(session) -> None:
    from app.models.certification import Certification

    for model in (AuditLog, Notification, Certification, TaskComment, TaskStatusHistory, TaskFile, TaskAssignee, Task,
                  Plan, OrderAcknowledgement, OrderFile, Order, User):
        await session.execute(delete(model))
    await session.commit()


# ------------------------------------------------------------------------ seed
async def seed_all(session, *, force: bool = False) -> bool:
    """Создаёт демо-данные. Возвращает True, если данные были созданы."""
    if force:
        await _wipe(session)

    existing = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if existing and not force:
        return False

    if not Path(settings.upload_dir).exists():
        Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)

    # ---- пользователи -------------------------------------------------------
    from app.models.position import Position

    users: dict[str, User] = {}
    pos_cache: dict[str, Position] = {}

    async def position_for(title: str) -> Position:
        """Должность из справочника: берём существующую или заводим новую."""
        key = title.strip()
        if key in pos_cache:
            return pos_cache[key]
        row = (await session.execute(select(Position).where(Position.title == key))).scalars().first()
        if row is None:
            row = Position(title=key, aliases="", sort_order=500)
            session.add(row)
            await session.flush()
        pos_cache[key] = row
        return row

    for spec in USERS:
        user = User(
            email=spec["email"],
            password_hash=hash_password(DEMO_PASSWORD),
            full_name=spec["full_name"],
            phone=spec.get("phone"),
            role=spec["role"],
            is_active=True,
            auth_source="local",     # локальная учётка: вход по паролю SchoolHub (break-glass)
        )
        # users.position больше нет: должность — запись справочника positions
        if spec.get("position"):
            user.positions = [await position_for(spec["position"])]
        session.add(user)
        users[spec["email"]] = user
    await session.flush()
    uid: dict[str, str] = {email: str(u.id) for email, u in users.items()}

    owner = users["owner@schoolhub.ru"]
    admin = users["admin@schoolhub.ru"]

    # ---- приказы ------------------------------------------------------------
    for spec in ORDERS:
        doc = DemoDoc(
            number=spec["number"],
            issued_on=spec["issued_on"],
            title=spec["title"],
            preamble=spec["preamble"],
            items=spec["items"],
            controls=spec.get("controls", []),
            kind="ПРИКАЗ",
        )
        if spec["fmt"] == "pdf":
            payload, mime, filename = build_pdf(doc), PDF_MIME, f"prikaz_{spec['number'].replace('-', '_')}.pdf"
        else:
            payload, mime, filename = build_docx(doc), DOCX_MIME, f"prikaz_{spec['number'].replace('-', '_')}.docx"

        if payload is None:  # reportlab/python-docx не установлены — минимальный корректный PDF
            payload, mime, filename = (
                b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
                b"trailer<</Root 1 0 R>>\n%%EOF\n",
                PDF_MIME,
                f"prikaz_{spec['number']}.pdf",
            )

        stored = await _save_file(payload, filename, mime)

        recipient_emails = spec.get("recipients")     # None = все сотрудники (кроме владельца)
        order = Order(
            id=uuid.uuid4(),
            number=spec["number"],
            order_year=spec["issued_on"].year,
            title=spec["title"],
            description=spec.get("description"),
            order_type=spec["order_type"],
            status=spec["status"],
            issued_on=spec["issued_on"],
            ack_deadline=spec.get("ack_deadline"),
            require_all=recipient_emails is None,
            recipient_ids=[uid[e] for e in recipient_emails] if recipient_emails else None,
            created_by_id=(owner if spec["order_type"] == OrderType.HR else admin).id,
            published_at=(
                _utc(datetime.combine(spec["issued_on"], dtime(9, 0)))
                if spec["status"] != OrderStatus.DRAFT else None
            ),
        )
        session.add(order)
        await session.flush()

        session.add(
            OrderFile(
                order_id=order.id,
                storage_path=stored.storage_path,
                original_name=stored.original_name,
                mime_type=stored.mime_type,
                size_bytes=stored.size_bytes,
                checksum=stored.checksum,
                version=1,
                is_current=True,
                uploaded_by_id=admin.id,
            )
        )

        # лист ознакомления: все активные кроме владельца ИЛИ выбранный круг
        if recipient_emails:
            recipients = [users[e] for e in recipient_emails]
        else:
            recipients = [u for u in users.values() if u.role != Role.OWNER and u.is_active]
        if spec["status"] in (OrderStatus.PUBLISHED, OrderStatus.ARCHIVED):
            acked_emails = spec.get("acked") or []
            for user in recipients:
                is_acked = acked_emails == "all" or user.email in acked_emails
                ack = OrderAcknowledgement(
                    order_id=order.id,
                    user_id=user.id,
                    status=AckStatus.ACKNOWLEDGED if is_acked else AckStatus.PENDING,
                    acknowledged_at=(
                        _utc(datetime.combine(spec["issued_on"] + timedelta(days=1), dtime(11, 30)))
                        if is_acked else None
                    ),
                    ip_address="10.0.12.34" if is_acked else None,
                    user_agent="SchoolHub-Seed/1.0" if is_acked else None,
                )
                session.add(ack)
                if not is_acked and spec["status"] == OrderStatus.PUBLISHED:
                    session.add(
                        Notification(
                            user_id=user.id,
                            type="ORDER_PUBLISHED",
                            title=f"Новый приказ №{order.number} — требуется ознакомление",
                            body=order.title,
                            entity_type="order",
                            entity_id=order.id,
                        )
                    )
    await session.flush()

    # ---- задачи (несколько исполнителей, отметки, вложения) ------------------
    for spec in TASKS:
        creator = users[spec["creator"]]
        assignees = [users[e] for e in spec["assignees"]]
        marks = {users[e].id for e in spec.get("marks", [])}
        deadline = _utc(spec["deadline"]) if spec.get("deadline") else None
        task = Task(
            id=uuid.uuid4(),
            title=spec["title"],
            description=spec.get("description"),
            status=spec["status"],
            priority=spec.get("priority", "NORMAL"),
            creator_id=creator.id,
            deadline=deadline,
            completed_at=_utc(spec["completed"]) if spec.get("completed") else None,
            recurrence=spec.get("recurrence", "NONE"),
            recurrence_weekdays=spec.get("recurrence_weekdays"),
            recurrence_day=spec.get("recurrence_day"),
        )
        completed = _utc(spec["completed"]) if spec.get("completed") else None
        for user in assignees:
            task.assignees.append(
                TaskAssignee(
                    user_id=user.id,
                    done_at=(completed or _utc(datetime.now(timezone.utc) - timedelta(days=1)))
                    if user.id in marks else None,
                )
            )
        session.add(task)
        await session.flush()

        session.add(
            TaskStatusHistory(task_id=task.id, from_status=None, to_status=TaskStatus.NEW.value, actor_id=creator.id)
        )
        if spec["status"] != TaskStatus.NEW:
            session.add(
                TaskStatusHistory(task_id=task.id, from_status=TaskStatus.NEW.value,
                                  to_status=spec["status"].value, actor_id=assignees[0].id)
            )
        for user in assignees:
            session.add(
                Notification(
                    user_id=user.id,
                    type="TASK_ASSIGNED",
                    title=f"Новая задача: {task.title}",
                    body=f"Постановщик: {creator.full_name}" + (
                        f" · исполнителей: {len(assignees)}" if len(assignees) > 1 else ""
                    ),
                    entity_type="task",
                    entity_id=task.id,
                )
            )
        for filename, mime, text in spec.get("files", []):
            if mime == XLSX_MIME:
                payload = _xlsx_stub(filename)
            else:
                payload = build_pdf(DemoDoc(number="ПРИЛ.", title=filename, preamble="", items=[text]))
                if payload is None:
                    payload = b"%PDF-1.4\ntrailer<</Root 1 0 R>>\n%%EOF\n"
            stored = await _save_file(payload, filename, mime, allowed=settings.allowed_task_file_mimes)
            session.add(
                TaskFile(
                    task_id=task.id,
                    storage_path=stored.storage_path,
                    original_name=stored.original_name,
                    mime_type=stored.mime_type,
                    size_bytes=stored.size_bytes,
                    checksum=stored.checksum,
                    uploaded_by_id=assignees[0].id,
                )
            )
        if spec.get("description"):
            session.add(
                TaskComment(
                    task_id=task.id,
                    author_id=creator.id,
                    body="Задача создана автоматически при seed'е демо-данных. Статус и сроки можно менять в интерфейсе.",
                )
            )
    await session.flush()

    # ---- планы работы на неделю ----------------------------------------------
    plans_spec = [
        # текущая неделя — опубликован
        dict(week_start=MONDAY, status=PlanStatus.PUBLISHED,
             published_days_ago=3, content=_plan_content(uid)),
        # прошлая неделя — архив
        dict(week_start=MONDAY - timedelta(days=7), status=PlanStatus.ARCHIVED,
             published_days_ago=10, content={
                 "duty": {"klass": "8 «А»", "teacher_id": uid["petrova@schoolhub.ru"]},
                 "notes": ["В столовой дежурят 6-ые классы."],
                 "month_items": [
                     {"text": "Контроль адаптации учеников 1-х, 5-х и 10-х классов (посещение уроков, диагностика).",
                      "place": None, "resp": [uid["admin@schoolhub.ru"], uid["malysheva@schoolhub.ru"]]}],
                 "week_items": [
                     {"text": "Подготовка к школьному этапу ВсОШ.", "place": "Кабинеты-предметные",
                      "resp": [uid["admin@schoolhub.ru"]]}],
                 "days": [
                     {"date": str(MONDAY - timedelta(days=7)), "events": [
                         {"title": "«Разговоры о важном».", "time": "08.00", "place": "Кл. каб.",
                          "resp": [uid["kuznecova@schoolhub.ru"], uid["petrova@schoolhub.ru"]]}]},
                     {"date": str(MONDAY - timedelta(days=6)), "events": [
                         {"title": "Совещание при директоре.", "time": "15.00", "place": "ЦПД",
                          "resp": [uid["owner@schoolhub.ru"], uid["admin@schoolhub.ru"], uid["tech@schoolhub.ru"]]}]},
                     {"date": str(MONDAY - timedelta(days=5)), "events": []},
                     {"date": str(MONDAY - timedelta(days=4)), "events": [
                         {"title": "«Россия — мои горизонты».", "time": "08.00", "place": "Кл. каб.",
                          "resp": [uid["avdeeva@schoolhub.ru"]]}]},
                     {"date": str(MONDAY - timedelta(days=3)), "events": []},
                     {"date": str(MONDAY - timedelta(days=2)), "events": [
                         {"title": "День учителя — праздничный концерт.", "time": "12.00", "place": "Акт. зал",
                          "resp": [uid["avdeeva@schoolhub.ru"], uid["sultanova@schoolhub.ru"]]}]},
                 ]}),
        # следующая неделя — черновик администрации
        dict(week_start=MONDAY + timedelta(days=7), status=PlanStatus.DRAFT,
             published_days_ago=None, content={
                 "duty": {"klass": None, "teacher_id": None},
                 "notes": [],
                 "month_items": [
                     {"text": "Контроль адаптации учеников 1-х, 5-х и 10-х классов (посещение уроков, диагностика).",
                      "place": None, "resp": [uid["admin@schoolhub.ru"], uid["malysheva@schoolhub.ru"]]}],
                 "week_items": [],
                 "days": [{"date": str(MONDAY + timedelta(days=7 + i)), "events": []} for i in range(6)]}),
    ]
    for spec in plans_spec:
        plan = Plan(
            id=uuid.uuid4(),
            week_start=spec["week_start"],
            week_end=Plan.compute_week_end(spec["week_start"]),
            status=spec["status"],
            content=spec["content"],
            created_by_id=admin.id,
            published_at=(
                _utc(datetime.now(timezone.utc) - timedelta(days=spec["published_days_ago"]))
                if spec["published_days_ago"] is not None else None
            ),
        )
        session.add(plan)
        await session.flush()
        if spec["status"] == PlanStatus.PUBLISHED:
            for rid in plan.responsible_ids():
                session.add(
                    Notification(
                        user_id=uuid.UUID(rid),
                        type="PLAN_PUBLISHED",
                        title=f"Опубликован план работы на неделю {plan.week_start:%d.%m.%Y}–{plan.week_end:%d.%m.%Y}",
                        body="Вы отмечены ответственным — откройте план, чтобы увидеть свои мероприятия",
                        entity_type="plan",
                        entity_id=plan.id,
                    )
                )
            session.add(
                AuditLog(actor_id=admin.id, actor_email=admin.email, actor_role=enum_value(admin.role),
                         action="PLAN_PUBLISH", entity_type="plan", entity_id=plan.id,
                         changes={"week": f"{plan.week_start:%d.%m.%Y} – {plan.week_end:%d.%m.%Y}",
                                  "events": plan.events_total(),
                                  "responsibles": len(plan.responsible_ids())})
            )
    await session.flush()

    # ---- аттестация и КПК (демо) ---------------------------------------------
    from app.models.certification import Certification

    today = date.today()
    certs_spec = [
        dict(email="ivanov@schoolhub.ru", kind="CATEGORY", category="Высшая",
             awarded_at=today - timedelta(days=1100), expires_at=None,
             document_no="приказ № 88-к", note="высшая категория устанавливается бессрочно"),
        dict(email="petrova@schoolhub.ru", kind="CATEGORY", category="Первая",
             awarded_at=today - timedelta(days=1800), expires_at=today + timedelta(days=30),
             document_no="приказ № 41-к"),
        dict(email="sherina@schoolhub.ru", kind="CATEGORY", category="Первая",
             awarded_at=today - timedelta(days=1900), expires_at=today - timedelta(days=20),
             document_no="приказ № 12-к", note="нужна переаттестация"),
        dict(email="smirnov@schoolhub.ru", kind="CPK", title="Цифровая грамотность педагога",
             hours=72, provider="ИРО", awarded_at=today - timedelta(days=200),
             expires_at=today + timedelta(days=400), document_no="уд. № 456"),
        dict(email="kuznecova@schoolhub.ru", kind="CPK", title="Оказание первой помощи",
             hours=36, provider="Центр ДПО", awarded_at=today - timedelta(days=400),
             expires_at=None, document_no="уд. № 789"),
    ]
    for cs in certs_spec:
        user = users.get(cs["email"])
        if user is None:
            continue
        session.add(Certification(
            user_id=user.id,
            kind=cs["kind"],
            category=cs.get("category"),
            title=cs.get("title"),
            hours=cs.get("hours"),
            provider=cs.get("provider"),
            document_no=cs.get("document_no"),
            awarded_at=cs.get("awarded_at"),
            expires_at=cs.get("expires_at"),
            note=cs.get("note"),
            created_by_id=owner.id,
        ))
    await session.flush()

    # ---- аудит --------------------------------------------------------------
    session.add(
        AuditLog(actor_id=owner.id, actor_email=owner.email, actor_role=enum_value(owner.role),
                 action="SYSTEM_SEED", entity_type=None,
                 changes={"users": len(USERS), "orders": len(ORDERS), "tasks": len(TASKS), "plans": len(plans_spec)})
    )
    await session.commit()
    return True


async def main(force: bool = False) -> None:
    await init_db()
    async with SessionLocal() as session:
        created = await seed_all(session, force=force)
    if created:
        print("✅ Демо-данные созданы.")
        print("   Учётные записи (пароль Demo1234!):")
        for spec in USERS:
            print(f"     {spec['role'].value:<8} {spec['email']:<26} {spec['full_name']}")
    else:
        print("ℹ️  Данные уже существуют — seed пропущен (используйте --force для пересоздания).")


if __name__ == "__main__":
    asyncio.run(main(force="--force" in sys.argv))

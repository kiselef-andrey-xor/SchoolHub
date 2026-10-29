"""Сервис справки/FAQ (модуль 4 ТЗ): карточки, поиск, seed системных статей, листы ознакомления.

Системные карточки (slug): password, roles, signature (ПЭП), lockdown — seed при старте;
редактирует только Владелец. Загружаемые инструкции — администрация.
Флаг mandatory_read создаёт лист ознакомления по образцу механизма приказов:
при включении — всем активным сотрудникам рассылаются PENDING-отметки + уведомление;
ознакомление — клик либо ПЭП (TOTP) в зависимости от флага require_code.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    HELP_VISIBILITY_MIN_ROLE,
    ROLE_LEVEL,
    AckSignMethod,
    HelpVisibility,
    Role,
    SignaturePolicy,
)
from app.errors import conflict, forbidden, not_found
from app.models.help import HelpAck, HelpArticle, HelpArticleFile
from app.models.user import User
from app.services import audit, notifications, totp

SYSTEM_CARDS: list[dict] = [
    {
        "slug": "password",
        "title": "Пароль и вход в систему",
        "category": "Аккаунт",
        "body": (
            "## Пароль\n\n"
            "- Минимум 10 символов, запрещены распространённые и совпадающие с логином.\n"
            "- После смены все активные сессии, кроме текущей, завершаются.\n"
            "- При входе с нового IP владелец может включить подтверждение.\n"
            "- Если пароль сброшен администратором — при первом входе система потребует сменить его."
        ),
    },
    {
        "slug": "roles",
        "title": "Роли и права",
        "category": "Аккаунт",
        "body": (
            "## Роли\n\n"
            "- **Учитель** — видит свои задачи, опубликованные приказы и планы, отметки «Ознакомлен».\n"
            "- **Администрация** — всё выше + журнал входящих, оборудование, замещения, согласование записок.\n"
            "- **Владелец** — полный доступ: пользователи, AD, аудит, локдаун, восстановление из копии."
        ),
    },
    {
        "slug": "signature",
        "title": "Электронная подпись (ПЭП через аутентификатор)",
        "category": "Документы",
        "body": (
            "## Уровни подписи\n\n"
            "1. **Без подписи** — только фиксация в аудите.\n"
            "2. **Клик «Ознакомлен»** — обычный уровень для большинства приказов.\n"
            "3. **Код из аутентификатора (TOTP)** — привяжите Google Authenticator/аналог в профиле.\n"
            "4. **Код + контрольное слово** — для критичных операций: восстановление из копии, "
            "очистка аудита, локдаун, удаление пользователя.\n\n"
            "Приказы о движении обучающихся всегда требуют усиленную подпись (код TOTP). "
            "Если аутентификатор не привязан — подписание блокируется с подсказкой; пароль вместо кода принять нельзя."
        ),
    },
    {
        "slug": "lockdown",
        "title": "Режим «Локдаун»",
        "category": "Безопасность",
        "body": (
            "## Локдаун\n\n"
            "Владелец может одним переключателем остановить вход и запись во всю систему "
            "(чтение остаётся доступным авторизованным). Выход из локдауна, как и вход в него, "
            "требуют код TOTP + контрольное слово. Все действия фиксируются в аудите."
        ),
    },
]


async def seed_system_articles(session: AsyncSession) -> int:
    """Создаёт недостающие системные карточки (идемпотентно; существующие не перезаписывает)."""
    created = 0
    for card in SYSTEM_CARDS:
        exists = (await session.execute(
            select(HelpArticle).where(HelpArticle.slug == card["slug"])
        )).scalar_one_or_none()
        if exists is None:
            session.add(HelpArticle(kind="SYSTEM", slug=card["slug"], title=card["title"],
                                    category=card["category"], body=card["body"]))
            created += 1
    if created:
        await session.flush()
    return created


# ------------------------------------------------------------------ видимость
def visibility_of(article: HelpArticle) -> HelpVisibility:
    """Значение видимости статьи (терпимо к None и к строке из старых выгрузок)."""
    value = getattr(article, "visibility", None)
    if isinstance(value, HelpVisibility):
        return value
    try:
        return HelpVisibility(value) if value else HelpVisibility.ALL
    except ValueError:
        return HelpVisibility.ALL


def visible_to(actor: User) -> list[HelpVisibility]:
    """Какие статьи видит роль: ALL — всем, ADMIN — администрации и Владельцу, OWNER — Владельцу."""
    level = ROLE_LEVEL[actor.role]
    return [v for v in HelpVisibility if level >= HELP_VISIBILITY_MIN_ROLE[v]]


def can_view(article: HelpArticle, actor: User) -> bool:
    return ROLE_LEVEL[actor.role] >= HELP_VISIBILITY_MIN_ROLE[visibility_of(article)]


def settable_by(actor: User) -> list[HelpVisibility]:
    """Какую видимость разрешено назначать этому пользователю.

    Администрация не создаёт статьи «только для Владельца»: такую инструкцию её же
    автор не сможет ни прочитать, ни исправить. Владельцу доступны все три значения.
    """
    if actor.role == Role.OWNER:
        return list(HelpVisibility)
    return [HelpVisibility.ALL, HelpVisibility.ADMIN]


def assert_can_set(actor: User, visibility: HelpVisibility) -> None:
    if visibility not in settable_by(actor):
        allowed = ", ".join(v.value for v in settable_by(actor))
        raise forbidden(
            f"Ваша роль не может задать видимость «{visibility.value}». Доступные значения: {allowed}"
        )


def can_edit(article: HelpArticle, actor: User) -> bool:
    if not can_view(article, actor):
        # статья чужой видимости для роли не существует — править её тоже нельзя
        return False
    if article.kind == "SYSTEM" or visibility_of(article) == HelpVisibility.OWNER:
        return actor.role == Role.OWNER
    return actor.role in (Role.OWNER, Role.ADMIN)


async def get_article(session: AsyncSession, article_id: uuid.UUID, actor: User | None = None) -> HelpArticle:
    art = await session.get(HelpArticle, article_id)
    if art is None:
        from app.errors import not_found
        raise not_found("Статья справки")
    if actor is not None and not can_view(art, actor):
        # 404, а не 403: сам факт существования служебной инструкции — информация,
        # которую не должен получать сотрудник без доступа
        from app.errors import not_found
        raise not_found("Статья справки")
    return art


def require_visible(article: HelpArticle, actor: User) -> None:
    """Проверка видимости для уже загруженной статьи (404 вместо 403)."""
    if not can_view(article, actor):
        from app.errors import not_found
        raise not_found("Статья справки")


async def list_articles(session: AsyncSession, actor: User, *, q: str | None = None,
                        category: str | None = None, kind: str | None = None,
                        visibility: HelpVisibility | None = None) -> list[HelpArticle]:
    """Список статей, доступных роли. Статьи чужой видимости не выдаются вовсе."""
    allowed = visible_to(actor)
    if visibility is not None:
        if visibility not in allowed:
            raise forbidden("Статьи с такой видимостью вам недоступны")
        allowed = [visibility]
    stmt = select(HelpArticle).where(HelpArticle.visibility.in_(allowed))
    if q:
        from app.dbutils import ilike
        stmt = stmt.where(or_(ilike(HelpArticle.title, q), ilike(HelpArticle.body, q),
                              ilike(HelpArticle.category, q)))
    if category:
        stmt = stmt.where(HelpArticle.category == category)
    if kind:
        stmt = stmt.where(HelpArticle.kind == kind)
    stmt = stmt.order_by(HelpArticle.category, HelpArticle.title)
    return list((await session.execute(stmt)).scalars().all())


async def categories(session: AsyncSession, actor: User) -> list[str]:
    """Категории только доступных статей: иначе список раскрывал бы наличие служебных разделов."""
    rows = (await session.execute(
        select(HelpArticle.category)
        .where(HelpArticle.visibility.in_(visible_to(actor)))
        .distinct()
    )).scalars().all()
    return sorted(rows)


async def create_article(session: AsyncSession, actor: User, *, title: str, category: str,
                         body: str | None, mandatory_read: bool,
                         visibility: HelpVisibility = HelpVisibility.ALL) -> HelpArticle:
    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Создавать инструкции может администрация")
    assert_can_set(actor, visibility)
    art = HelpArticle(kind="UPLOAD", title=title.strip(), category=(category or "Общее").strip(),
                      body=body, mandatory_read=mandatory_read, visibility=visibility,
                      created_by_id=actor.id, updated_by_id=actor.id)
    session.add(art)
    await session.flush()
    if mandatory_read:
        await _open_ack_sheet(session, art, actor)
    await audit.write_audit(
        session, actor=actor, action="HELP_CREATE", entity_type="help_article", entity_id=art.id,
        changes={"title": art.title, "category": art.category, "mandatory_read": mandatory_read,
                 "visibility": visibility.value},
    )
    return art


async def update_article(session: AsyncSession, actor: User, art: HelpArticle, *, title: str | None = None,
                         category: str | None = None, body: str | None = None,
                         mandatory_read: bool | None = None,
                         visibility: HelpVisibility | None = None) -> HelpArticle:
    if not can_edit(art, actor):
        raise forbidden("Редактировать эту статью нельзя")
    was_mandatory = art.mandatory_read
    was_visibility = visibility_of(art)
    if title is not None and title.strip():
        art.title = title.strip()
    if category is not None and category.strip():
        art.category = category.strip()
    if body is not None:
        art.body = body
    if mandatory_read is not None:
        art.mandatory_read = mandatory_read
    if visibility is not None:
        assert_can_set(actor, visibility)
        art.visibility = visibility
    art.updated_by_id = actor.id
    await session.flush()
    if mandatory_read and not was_mandatory:
        await _open_ack_sheet(session, art, actor)
    elif art.mandatory_read and visibility is not None and visibility != was_visibility:
        # круг читателей изменился: лист ознакомления приводим в соответствие,
        # иначе у потерявших доступ сотрудников висела бы вечная «не прочитано»
        await _rescope_ack_sheet(session, art, actor)
    await audit.write_audit(
        session, actor=actor, action="HELP_UPDATE", entity_type="help_article", entity_id=art.id,
        changes={"title": art.title, "mandatory_read": art.mandatory_read,
                 "visibility": visibility_of(art).value},
    )
    return art


async def delete_article(session: AsyncSession, actor: User, art: HelpArticle) -> None:
    if not can_edit(art, actor):
        raise forbidden("Удалять эту статью нельзя")
    if art.kind == "SYSTEM":
        raise conflict("Системные карточки удалять нельзя")

    # файлы убираем ДО удаления строки: cascade очистит связи, но сами байты
    # останутся в хранилище и будут числиться сиротами в сводке «Файлы школы»
    from app.services import storage as _storage_mod

    for row in list(art.files):
        try:
            _storage_mod.storage.delete_pdf_pair(row.storage_path, row.pdf_path)
        except Exception:  # noqa: BLE001 — статья уже удаляется, файл не критичен
            pass
    # наследие до v1.12: единственный файл в колонках help_articles.file_*
    if art.file_path:
        try:
            _storage_mod.storage.delete(art.file_path)
        except Exception:  # noqa: BLE001
            pass

    await session.delete(art)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="HELP_DELETE", entity_type="help_article", entity_id=art.id,
        changes={"title": art.title},
    )


# ----------------------------------------------------------- лист ознакомления
async def _ack_recipients(session: AsyncSession, art: HelpArticle) -> list[User]:
    """Кому адресована инструкция: активные сотрудники нужного уровня роли.

    Видимость статьи ограничивает и лист ознакомления: инструкция «только для
    администрации» не создаёт отметок у учителей — иначе у них висел бы
    неудаляемый долг «не ознакомлен» с документом, которого они не видят.
    """
    min_level = HELP_VISIBILITY_MIN_ROLE[visibility_of(art)]
    users = (await session.execute(
        select(User).where(User.is_active.is_(True), User.is_deleted.is_(False))
    )).scalars().all()
    return [u for u in users if ROLE_LEVEL[u.role] >= min_level]


async def _open_ack_sheet(session: AsyncSession, art: HelpArticle, actor: User) -> None:
    """Включение «обязательно к прочтению» = лист ознакомления по механизму приказов."""
    users = await _ack_recipients(session, art)
    existing = {a.user_id for a in (await session.execute(
        select(HelpAck).where(HelpAck.article_id == art.id)
    )).scalars().all()}
    opened = 0
    for u in users:
        if u.id not in existing:
            session.add(HelpAck(article_id=art.id, user_id=u.id))
            opened += 1
    await session.flush()
    await notifications.push_many(
        session, user_ids=[u.id for u in users if u.id != actor.id], type_="HELP_MANDATORY",
        title=f"Обязательно к прочтению: {art.title}",
        body="Инструкция добавлена в ваш список ознакомления.",
        entity_type="help_article", entity_id=art.id,
    )
    await audit.write_audit(
        session, actor=actor, action="HELP_SHEET_OPEN", entity_type="help_article", entity_id=art.id,
        changes={"title": art.title, "recipients": opened, "visibility": visibility_of(art).value},
    )


async def _rescope_ack_sheet(session: AsyncSession, art: HelpArticle, actor: User) -> None:
    """Приводит лист ознакомления к новому кругу читателей после смены видимости."""
    users = await _ack_recipients(session, art)
    allowed = {u.id for u in users}
    rows = (await session.execute(
        select(HelpAck).where(HelpAck.article_id == art.id)
    )).scalars().all()
    existing = {r.user_id for r in rows}

    removed = 0
    for row in rows:
        if row.user_id not in allowed:
            await session.delete(row)
            removed += 1
    added = 0
    for u in users:
        if u.id not in existing:
            session.add(HelpAck(article_id=art.id, user_id=u.id))
            added += 1
    await session.flush()
    if added:
        await notifications.push_many(
            session, user_ids=[u.id for u in users if u.id not in existing and u.id != actor.id],
            type_="HELP_MANDATORY",
            title=f"Обязательно к прочтению: {art.title}",
            body="Инструкция добавлена в ваш список ознакомления.",
            entity_type="help_article", entity_id=art.id,
        )
    await audit.write_audit(
        session, actor=actor, action="HELP_SHEET_RESCOPE", entity_type="help_article", entity_id=art.id,
        changes={"title": art.title, "visibility": visibility_of(art).value,
                 "recipients": added, "removed": removed},
    )


async def ack_article(session: AsyncSession, actor: User, art: HelpArticle, *, code: str | None,
                      word: str | None, ip: str | None, ua: str | None,
                      require_code: bool) -> HelpAck:
    """Отметка «Ознакомлен»: клик или ПЭП (TOTP), если инструкция требует код."""
    require_visible(art, actor)
    if not art.mandatory_read:
        raise conflict("Статья не помечена обязательной к прочтению")
    row = (await session.execute(
        select(HelpAck).where(HelpAck.article_id == art.id, HelpAck.user_id == actor.id)
    )).scalar_one_or_none()
    if row is None:
        # сотрудник принят после рассылки — допускаемся к листу автоматически
        row = HelpAck(article_id=art.id, user_id=actor.id)
        session.add(row)
    if row.acked_at is not None:
        raise conflict("Вы уже ознакомились с этой инструкцией")

    policy = SignaturePolicy.TOTP.value if require_code else SignaturePolicy.CLICK.value
    method = await totp.enforce_signature(session, actor, policy, code, word)
    row.acked_at = datetime.now(timezone.utc)
    row.sign_method = method.value if isinstance(method, AckSignMethod) else str(method)
    row.ip_address = ip
    row.user_agent = (ua[:512] if ua else None)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="HELP_ACK", entity_type="help_article", entity_id=art.id,
        changes={"title": art.title, "sign_method": row.sign_method},
    )
    return row


# ----------------------------------------------------------- файл инструкции
async def attach_file(session: AsyncSession, actor: User, art: HelpArticle, file) -> HelpArticle:
    """Прикрепляет (заменяет) файл инструкции PDF/DOCX во встроенном хранилище."""
    from app.config import settings
    from app.services import storage as _storage_mod

    if not can_edit(art, actor):
        raise forbidden("Загружать файлы к этой статье нельзя")
    mime = (file.content_type or "").lower()
    stored = await _storage_mod.storage.save(file.file, file.filename or "file.bin", mime,
                                allowed_mimes=settings.allowed_task_file_mimes)
    old_path = art.file_path
    art.file_path = stored.storage_path
    art.file_name = stored.original_name
    art.file_mime = stored.mime_type
    art.file_size = stored.size_bytes
    art.updated_by_id = actor.id
    await session.flush()
    if old_path and old_path != stored.storage_path:
        try:
            _storage_mod.storage.delete(old_path)
        except Exception:  # noqa: BLE001 — старый файл не критичен
            pass
    await audit.write_audit(
        session, actor=actor, action="HELP_FILE", entity_type="help_article", entity_id=art.id,
        changes={"file": art.file_name, "size": art.file_size},
    )
    return art


async def ack_sheet(session: AsyncSession, actor: User, art: HelpArticle) -> list[HelpAck]:
    if not can_edit(art, actor):
        raise forbidden("Лист ознакомления виден администрации")
    stmt = select(HelpAck).where(HelpAck.article_id == art.id).order_by(HelpAck.acked_at.desc().nullslast())
    return list((await session.execute(stmt)).scalars().all())


async def my_pending_count(session: AsyncSession, actor: User) -> int:
    """Счётчик для бейджа «Справка»: сколько обязательных инструкций не прочитано.

    Видимость учитывается здесь же: после сужения круга читателей у потерявшего
    доступ сотрудника не должно оставаться «долгов» по невидимой инструкции.
    """
    base = (
        select(HelpAck.id)
        .join(HelpArticle, HelpArticle.id == HelpAck.article_id)
        .where(HelpAck.user_id == actor.id, HelpAck.acked_at.is_(None),
               HelpArticle.mandatory_read.is_(True),
               HelpArticle.visibility.in_(visible_to(actor)))
    )
    return len((await session.execute(base)).all())


# ------------------------------------------------------------------- вложения
async def refresh_pending_pdfs(article: HelpArticle) -> int:
    """Доделывает PDF-копии документов инструкции, зависшие в PENDING.

    Сценарий: контейнер перезапустили между загрузкой и фоновой конверсией —
    BackgroundTasks живёт только в пределах запроса, поэтому копия осталась бы
    «готовится» навсегда. Возвращает число доделанных файлов.
    """
    from app.enums import PdfStatus
    from app.services import document_pdf

    done = 0
    for row in article.files:
        if row.pdf_status == PdfStatus.PENDING:
            await document_pdf.ensure_pdf(row)
            done += 1
    return done


async def add_file(session: AsyncSession, actor: User, article: HelpArticle, file,
                   caption: str | None = None) -> HelpArticleFile:
    """Прикрепить документ к инструкции (v1.12: файлов может быть НЕСКОЛЬКО).

    Прежняя модель «один файл в колонках help_articles.file_*» не позволяла
    приложить к инструкции, например, и положение, и форму заявления к нему.

    PDF-копия готовится ФОНОМ после ответа клиенту (см. API), чтобы загрузка
    документа не ждала LibreOffice.
    """
    from app.config import settings
    from app.services import document_pdf
    from app.services import storage as _storage_mod

    # доступ проверяет и API (ManagerDep = require_role(ADMIN)), и сервис: статья
    # Владельца редактируется только Владельцем (can_edit), поэтому одного
    # require_role недостаточно
    if not can_edit(article, actor):
        raise forbidden("Прикреплять документы к этой инструкции нельзя")
    mime = (file.content_type or "").lower().split(";")[0].strip()
    # список MIME — как у вложений задач (шире «документных»): к инструкции
    # прикладывают и скан-фотографию, и выгрузку CSV, и архив с шаблонами
    stored = await _storage_mod.storage.save(
        file.file, file.filename or "instruction.bin", mime,
        allowed_mimes=settings.allowed_task_file_mimes,
    )
    plan = document_pdf.plan_for(stored.mime_type)
    row = HelpArticleFile(
        article_id=article.id, storage_path=stored.storage_path, original_name=stored.original_name,
        mime_type=stored.mime_type, size_bytes=stored.size_bytes, checksum=stored.checksum,
        caption=(caption or "").strip()[:300] or None,
        pdf_status=plan.status, pdf_note=plan.note,
        uploaded_by_id=actor.id,
    )
    session.add(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="HELP_FILE", entity_type="help_article", entity_id=article.id,
        changes={"title": article.title, "file": row.original_name, "size": row.size_bytes,
                 "caption": row.caption},
    )
    return row


async def delete_file(session: AsyncSession, actor: User, article: HelpArticle,
                      file_id: uuid.UUID) -> None:
    if not can_edit(article, actor):
        raise forbidden("Удалять документы этой инструкции нельзя")
    row = next((f for f in article.files if f.id == file_id), None)
    if row is None:
        raise not_found("Файл")
    from app.services import storage as _storage_mod

    # пара «оригинал + PDF-копия»: копия лежит отдельным файлом в хранилище
    _storage_mod.storage.delete_pdf_pair(row.storage_path, row.pdf_path)
    article.files.remove(row)
    await session.flush()
    await audit.write_audit(
        session, actor=actor, action="HELP_FILE_DELETE", entity_type="help_article",
        entity_id=article.id, changes={"title": article.title, "file": row.original_name},
    )

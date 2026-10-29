"""Печатная форма служебной записки (v1.12 — макет переделан полностью).

Прежняя форма была «отладочной»: одна строка с номером, тема заголовком, текст
в <pre>, список вложений и виза в правом углу. Печатать такое было нельзя —
документ не похож ни на один школьный бланк, а <pre> ломал переносы на A4.

Новый макет повторяет реальную служебную записку школы:

    ┌ шапка: наименование школы (слева) │ адресат (справа, в дательном падеже)
    ┌ СЛУЖЕБНАЯ ЗАПИСКА  ·  № 12/2026  ·  от 29.10.2026
    ┌ тема (полужирным, отдельной строкой)
    ┌ текст записки
    ┌ Приложение: на N л. в 1 экз. + перечень документов
    ┌ исполнитель и срок
    ┌ подпись автора: должность · подпись · ФИО
    ┌ СОГЛАСОВАНО: кто, когда, способ подписи (виза)
    ┌ отметка об исполнении: порождённая задача
    ┌ служебный колонтитул: id документа и момент формирования (для сверки)

Безопасность
------------
Форма открывается в сессии согласующего, а содержимое (тема, текст, имена
файлов) пишет ЛЮБОЙ сотрудник. Поэтому каждое значение проходит `htmlsafe.esc`
— без этого stored XSS (review/REVIEW.md §P0-2). ФИО дополнительно идут через
`lockdown.disp()`, чтобы режим «Локдаун» не раскрыл персональные данные.

Разметка собирается здесь, а не в `app/api/service_notes.py`: хендлер отвечает
за права и выборку, а за внешний вид документа — этот модуль.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import htmlsafe
from app.enums import AckSignMethod
from app.htmlsafe import esc
from app.models.service_note import ServiceNote
from app.models.user import User
from app.services import lockdown
from app.types import enum_value

_SIGN_RU = {
    AckSignMethod.NONE.value: "без подписи (фиксация в журнале)",
    AckSignMethod.CLICK.value: "клик «Ознакомлен»",
    AckSignMethod.TOTP.value: "одноразовый код аутентификатора (ПЭП)",
    AckSignMethod.CODE_WORD.value: "код аутентификатора + контрольное слово (усиленная ПЭП)",
}

_STATUS_RU = {
    "DRAFT": "черновик", "ON_APPROVAL": "на согласовании", "APPROVED": "утверждена",
    "REJECTED": "отклонена", "COMPLETED": "выполнена",
}

#: Стили документа. `@page` задаёт поля A4, чтобы браузер не добавлял свои
#: и записка помещалась на лист без «уехавшей» на второй страницы визы.
STYLE = """
@page { size: A4; margin: 18mm 15mm 20mm 25mm; }
body {
  font-family: 'Times New Roman', Times, Georgia, serif;
  font-size: 12.5pt; line-height: 1.45; color: #000; margin: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.note { max-width: 180mm; margin: 0 auto; }

/* --- шапка --- */
.note__top { display: flex; justify-content: space-between; align-items: flex-start; gap: 10mm; }
.note__org { flex: 1 1 auto; font-size: 10.5pt; line-height: 1.3; color: #1a1a1a; }
.note__org-name { font-weight: 700; font-size: 11.5pt; }
.note__org-meta { color: #444; }
.note__addressee { flex: 0 0 78mm; text-align: left; font-size: 11.5pt; line-height: 1.35; }
.note__addressee-role { font-style: italic; }
.note__addressee-name { font-weight: 700; }
.note__addressee-from { margin-top: 2mm; font-size: 11pt; }

.note__rule { border: 0; border-top: 1.4pt solid #000; margin: 5mm 0 1mm; }
.note__rule--thin { border-top: 0.5pt solid #000; margin-top: 0.6mm; }

/* --- заголовочная часть --- */
.note__kind { text-align: center; font-size: 10pt; letter-spacing: 0.14em;
  text-transform: uppercase; color: #333; margin-top: 6mm; }
.note__title { text-align: center; font-size: 17pt; font-weight: 700;
  letter-spacing: 0.06em; margin: 1.5mm 0 2mm; }
.note__regs { text-align: center; font-size: 11pt; color: #222; margin-bottom: 1mm; }
.note__regs b { font-weight: 700; }

/* --- тело --- */
.note__subject { font-weight: 700; margin: 7mm 0 3mm; text-align: center; font-size: 13pt; }
.note__body { white-space: pre-wrap; text-align: justify; hyphens: auto; margin: 0 0 4mm;
  text-indent: 12mm; }
.note__body--flat { text-indent: 0; }

.note__section-title { font-weight: 700; font-size: 11.5pt; margin: 5mm 0 1.5mm;
  text-transform: uppercase; letter-spacing: 0.05em; }
.note__attachments { margin: 0 0 0 6mm; padding: 0; list-style: none; counter-reset: att; }
.note__attachments li { counter-increment: att; margin-bottom: 1mm; font-size: 11.5pt; }
.note__attachments li::before { content: counter(att) ". "; }
.note__att-meta { color: #444; font-size: 10pt; }

.note__facts { width: 100%; border-collapse: collapse; margin: 3mm 0 0; font-size: 11.5pt; }
.note__facts td { border: 0.5pt solid #666; padding: 1.6mm 2.5mm; vertical-align: top; }
.note__facts td.k { width: 42mm; background: #f2f2f2; font-weight: 700; }

/* --- подписи --- */
.note__sign { margin-top: 12mm; }
.note__sign-row { display: flex; align-items: flex-end; gap: 4mm; }
.note__sign-role { flex: 1 1 auto; font-size: 11.5pt; }
.note__sign-line { flex: 0 0 42mm; border-bottom: 0.6pt solid #000; height: 5mm; }
.note__sign-name { flex: 0 0 62mm; font-size: 11.5pt; }
.note__sign-hint { display: flex; gap: 4mm; font-size: 8.5pt; color: #555; margin-top: 1mm; }
.note__sign-hint span:nth-child(2) { flex: 0 0 42mm; text-align: center; }
.note__sign-hint span:nth-child(3) { flex: 0 0 62mm; text-align: center; }
.note__sign-hint span:first-child { flex: 1 1 auto; }

.note__visa { margin-top: 10mm; border: 0.8pt solid #000; padding: 3mm 4mm; page-break-inside: avoid; }
.note__visa--none { border-style: dashed; border-color: #888; color: #333; }
.note__visa-title { font-weight: 700; font-size: 11pt; letter-spacing: 0.08em; margin-bottom: 2mm; }
.note__visa table { width: 100%; border-collapse: collapse; font-size: 10.5pt; }
.note__visa td { padding: 0.7mm 0; vertical-align: top; }
.note__visa td.k { width: 40mm; color: #333; }
.note__stamp { float: right; margin: 0 0 2mm 4mm; width: 46mm; height: 24mm; border: 0.6pt dashed #999;
  border-radius: 50%; color: #999; font-size: 8pt; text-align: center; line-height: 24mm; }

.note__exec { margin-top: 8mm; font-size: 10.5pt; color: #222; border-left: 2.5pt solid #333;
  padding: 1.5mm 0 1.5mm 3mm; page-break-inside: avoid; }
.note__footer { margin-top: 10mm; border-top: 0.5pt solid #999; padding-top: 1.5mm;
  font-size: 8pt; color: #666; display: flex; justify-content: space-between; gap: 6mm; }
.note__footer .mono { font-family: 'Courier New', monospace; }
.note__warn { margin-top: 6mm; font-size: 10pt; color: #7a1010; border: 0.8pt solid #7a1010;
  padding: 2mm 3mm; }
.toolbar { position: fixed; top: 0; right: 0; padding: 8px; }
.toolbar button { font: 12pt system-ui, sans-serif; padding: 6px 14px; cursor: pointer; }
@media print { .toolbar { display: none !important; } }
@media screen { body { padding: 16mm 8mm; background: #fff; } .note { box-shadow: 0 0 0 0.5pt #ccc; } }
"""


def _dt(value: datetime | None, with_time: bool = False) -> str:
    if value is None:
        return "—"
    local = value.astimezone() if value.tzinfo else value
    return local.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def _name(user: User | None, user_id) -> str:
    """ФИО с учётом режима «Локдаун» (персональные данные подменяются метками)."""
    return lockdown.disp(user.full_name if user else None, user_id) or "—"


def _positions(user: User | None) -> str:
    """Должность сотрудника из справочника (может быть несколько)."""
    if user is None:
        return ""
    titles = [p.title for p in (getattr(user, "positions", None) or []) if getattr(p, "title", None)]
    return ", ".join(titles)


def _dative(full_name: str) -> str:
    """ФИО в дательном падеже для адресата: «Иванову Ивану Ивановичу».

    Полноценного склонения по падежам в проекте нет (и добавлять морфологический
    словарь ради печатной формы неоправданно), поэтому применяется простое
    школьное правило для русских ФИО: фамилия на -ов/-ев/-ин/-ын → -у, на -ий → -ому,
    на -ь/й (мужские) → -ю; имя и отчество на -а/-я → -е/-и, на -ий → -ию.
    Женские фамилии (-а, -ая) и нерусские окончания не трогаем: ошибка склонения
    в фамилии заметнее, чем отсутствие склонения.
    """
    parts = (full_name or "").split()
    if not parts:
        return full_name or "—"

    def word(text: str, *, is_surname: bool) -> str:
        low = text.lower()
        if low.endswith("ич"):                      # отчество: …-ич → …-ичу
            return text + "у"
        if low.endswith("ий"):
            return text[:-2] + ("ому" if is_surname else "ию")
        if low.endswith("ия"):
            return text[:-2] + "ии"
        if low.endswith("ья"):
            return text[:-2] + "ье"
        if low.endswith("я"):
            return text[:-1] + ("и" if is_surname else "ю")
        if low.endswith("а") and not low.endswith(("иа", "оа")):
            return text[:-1] + ("ой" if is_surname else "е")
        if low.endswith(("ов", "ев", "ёв", "ин", "ын", "ун", "ан")):
            return text + "у" if is_surname else text
        if low.endswith(("ый", "ий", "ой")) and is_surname:
            return text[:-2] + "ому"
        if low.endswith(("б", "в", "г", "д", "ж", "з", "к", "л", "м", "н", "п", "р",
                         "с", "т", "ф", "х", "ц", "ч", "ш", "щ")) and not low.endswith("ь"):
            return text + "у" if is_surname else text
        return text

    surname = word(parts[0], is_surname=True)
    rest = [word(p, is_surname=False) for p in parts[1:]]
    return " ".join([surname, *rest])


def _addressee(note: ServiceNote, school_name: str | None) -> str:
    """Правый верхний угол: кому адресована записка и от кого."""
    approver = note.approver
    if approver is not None:
        role = _positions(approver) or ("директору школы" if enum_value(approver.role) == "OWNER" else "администрации")
        who = f"{esc(role)}<br><span class='note__addressee-name'>{esc(_dative(_name(approver, note.approver_id)))}</span>"
    else:
        who = ("<span class='note__addressee-role'>руководителю образовательной организации</span><br>"
               "<span class='note__addressee-name'>_______________________</span>")
    author = note.author
    author_role = _positions(author) or "сотруднику"
    return (
        f"<div class='note__addressee'>{who}"
        f"<div class='note__addressee-from'>от {esc(_dative(author_role))}<br>"
        f"<b>{esc(_name(author, note.author_id))}</b></div></div>"
    )


def _attachments(note: ServiceNote) -> str:
    """Перечень приложений. PDF-копия помечается — печатать удобно именно её."""
    if not note.files:
        return ""
    items = []
    for f in note.files:
        size = f"{f.size_bytes / 1024:.0f} КБ" if (f.size_bytes or 0) < 1024 * 1024 \
            else f"{(f.size_bytes or 0) / 1024 / 1024:.1f} МБ"
        pdf = " · PDF-копия готова" if getattr(f, "pdf_ready", False) else ""
        items.append(
            f"<li>{esc(f.original_name)} "
            f"<span class='note__att-meta'>({esc(size)}{esc(pdf)})</span></li>"
        )
    count = len(items)
    tail = "л." if count == 1 else "л."
    return (
        f"<div class='note__section-title'>Приложение</div>"
        f"<div class='note__body note__body--flat'>на {count} {tail} в 1 экз.:</div>"
        f"<ol class='note__attachments'>{''.join(items)}</ol>"
    )


def _visa(note: ServiceNote) -> str:
    """Виза согласования: кто подписал, когда, каким способом, плюс место для печати."""
    if not note.approved_at:
        status = _STATUS_RU.get(enum_value(note.status), enum_value(note.status))
        return (
            "<div class='note__visa note__visa--none'>"
            "<div class='note__visa-title'>СОГЛАСОВАНО</div>"
            f"<table><tr><td class='k'>Статус документа</td><td>{esc(status)} — визы нет</td></tr>"
            "<tr><td class='k'>Место для визы</td>"
            "<td>____________________ / ____________________ / «____» ____________ 20____ г.</td></tr>"
            "</table></div>"
        )
    sign = _SIGN_RU.get(note.approved_sign_method or "", note.approved_sign_method or "—")
    approver = note.approver
    rows = [
        ("Согласовал(а)", f"{esc(_name(approver, note.approver_id))}"
                          + (f" — {esc(_positions(approver))}" if _positions(approver) else "")),
        ("Дата и время", esc(_dt(note.approved_at, with_time=True))),
        ("Способ подписи", esc(sign)),
        ("Документ утверждён", "да, создана задача исполнителю" if note.task_id else "да"),
    ]
    body = "".join(f"<tr><td class='k'>{k}</td><td>{v}</td></tr>" for k, v in rows)
    return (
        "<div class='note__visa'><div class='note__stamp'>М.П.</div>"
        f"<div class='note__visa-title'>СОГЛАСОВАНО</div><table>{body}</table></div>"
    )


def _execution(note: ServiceNote) -> str:
    """Отметка об исполнении: порожённая задача — след записки в Task Manager."""
    if not note.task_id:
        return ""
    executor = note.executor
    return (
        "<div class='note__exec'><b>Отметка об исполнении.</b> "
        f"По настоящей записке создана задача исполнителю "
        f"{esc(_name(executor, note.executor_id))}"
        f"{f' со сроком до {esc(_dt(note.deadline))}' if note.deadline else ''}. "
        f"Идентификатор задачи: <span class='mono'>{esc(str(note.task_id))}</span>. "
        "Отметка о выполнении проставляется исполнителем в системе.</div>"
    )


def _reject_block(note: ServiceNote) -> str:
    if enum_value(note.status) != "REJECTED" or not note.reject_comment:
        return ""
    return (f"<div class='note__warn'><b>Записка отклонена.</b> "
            f"Комментарий согласующего: {esc(note.reject_comment)}</div>")


async def render_async(session, note: ServiceNote, *, printed_by=None,
                       auto_print: bool = False) -> str:
    """То же, что `render()`, но подтягивает наименование школы из `setup_state`.

    Название задаёт Владелец (Профиль → «Данные школы») и оно же печатается в
    угловом штампе. Отдельный async-хелпер — чтобы хендлер не знал про таблицу
    настройки и не повторял запрос в каждой печатной форме.
    """
    from sqlalchemy import select

    from app.models.setup import SetupState

    name = (await session.execute(select(SetupState.school_name))).scalar_one_or_none()
    return render(note, school_name=name, printed_by=printed_by, auto_print=auto_print)


def render(note: ServiceNote, *, school_name: str | None = None,
           printed_by: User | None = None, auto_print: bool = False) -> str:
    """Собирает полный HTML-документ печатной формы служебной записки.

    `auto_print=False` по умолчанию: форму открывают в новой вкладке, чтобы
    прочитать и распечатать по кнопке — немедленный диалог печати мешал бы
    просмотреть документ перед отправкой на принтер.
    """
    org = (
        f"<div class='note__org'><div class='note__org-name'>{esc(school_name.strip())}</div>"
        f"<div class='note__org-meta'>единая цифровая среда SchoolHub</div></div>"
        if (school_name or "").strip()
        else "<div class='note__org'><div class='note__org-name'>_______________________________</div>"
             "<div class='note__org-meta'>наименование образовательной организации</div></div>"
    )
    number = f"№ {esc(str(note.number))}/{esc(str(note.year))}" if note.number else "б/н"
    facts = [
        ("Автор записки", esc(_name(note.author, note.author_id))
                          + (f" — {esc(_positions(note.author))}" if _positions(note.author) else "")),
        ("Исполнитель", esc(_name(note.executor, note.executor_id))),
        ("Срок исполнения", esc(_dt(note.deadline))),
        ("Дата составления", esc(_dt(note.created_at))),
    ]
    facts_html = "".join(f"<tr><td class='k'>{k}</td><td>{v}</td></tr>" for k, v in facts)

    body = (
        "<div class='note'>"
        f"<div class='note__top'>{org}{_addressee(note, school_name)}</div>"
        "<hr class='note__rule'><hr class='note__rule note__rule--thin'>"
        "<div class='note__kind'>внутренний документ</div>"
        "<div class='note__title'>СЛУЖЕБНАЯ ЗАПИСКА</div>"
        f"<div class='note__regs'>{number} от <b>{esc(_dt(note.created_at))}</b>"
        f" &nbsp;·&nbsp; г. {esc(str(note.year or ''))}</div>"
        f"<div class='note__subject'>{esc(note.subject)}</div>"
        f"<div class='note__body'>{esc(note.body or '')}</div>"
        f"{_attachments(note)}"
        f"<div class='note__section-title'>Реквизиты</div>"
        f"<table class='note__facts'>{facts_html}</table>"
        f"{_reject_block(note)}"
        "<div class='note__sign'>"
        "<div class='note__sign-row'>"
        f"<div class='note__sign-role'>{esc(_positions(note.author) or 'Автор записки')}</div>"
        "<div class='note__sign-line'></div>"
        f"<div class='note__sign-name'>{esc(_name(note.author, note.author_id))}</div>"
        "</div>"
        "<div class='note__sign-hint'><span>должность</span><span>подпись</span><span>расшифровка</span></div>"
        "</div>"
        f"{_visa(note)}"
        f"{_execution(note)}"
        "<div class='note__footer'>"
        f"<span>Документ сформирован {_esc(_dt(datetime.now(timezone.utc), with_time=True))}"
        f"{_esc(' · ' + lockdown.disp(printed_by.full_name, printed_by.id)) if printed_by else ''}</span>"
        f"<span class='mono'>id {esc(str(note.id))}</span>"
        "</div>"
        "</div>"
        "<div class='toolbar'><button onclick=\"window.print()\">🖨 Печать</button></div>"
    )
    # Кнопка «Печать» — inline-обработчик, поэтому скрипты разрешены точечно
    # (allow_script) и при auto_print=False: форму открывают в новой вкладке,
    # чтобы сначала прочитать, а печать запускают рукой. Внешние ресурсы
    # запрещены политикой htmlsafe._PRINT_CSP в обоих случаях.
    return htmlsafe.page(
        f"Служебная записка {note.number}/{note.year}" if note.number else "Служебная записка",
        body, extra_style=STYLE, auto_print=auto_print, allow_script=True,
    )


def _esc(value: str) -> str:
    """Отдельная обёртка: значение собирается конкатенацией и должно быть
    экранировано ПОСЛЕ склейки, а не до неё."""
    return esc(value)

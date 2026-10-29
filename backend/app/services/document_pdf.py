"""Автоматическая PDF-копия загруженного документа (v1.12).

Задача модуля: **любой** документ, приложенный к служебной записке, заявке
Helpdesk или инструкции, должен открываться по клику в новой вкладке браузера.
Браузеры умеют показывать только PDF и изображения — ни DOCX, ни XLSX, ни TXT
они не рендерят. Поэтому при загрузке рядом с оригиналом готовится PDF-копия.

Что отличается от `app/services/documents.py` (там — кэш `.preview.pdf` для
инлайн-превью приказов):

  1. **Копия — самостоятельный файл хранилища** со своим `storage_path`, а не
     `.preview.pdf` рядом. Она переживает пересборку образа, попадает в архив
     «Файлы школы» и передаётся вместе с документом (служебка → задача).
  2. **Конвертируются не только офисные форматы**: изображения и текст
     упаковываются в PDF через ReportLab, поэтому «фото зажёванного принтера»
     в заявке Helpdesk тоже открывается в новой вкладке.
  3. **Состояние фиксируется в БД** (`pdf_status`, `pdf_note`): UI честно
     показывает «PDF-копия готовится», «не конвертируется (архив)» или
     «конвертер недоступен — скачайте оригинал», а не крутит пустой iframe.

Оригинал НЕ удаляется и остаётся доступным для скачивания: PDF нужен для
чтения и печати, исходник — чтобы отдать документ в авторском формате.

Блокировка event loop
---------------------
Все конвертеры синхронные (`subprocess.run`, ReportLab). Из async-кода вызывать
ТОЛЬКО `ensure_pdf()` — он уносит работу в поток через `asyncio.to_thread`.
Прямой вызов `build_pdf()` из хендлера остановил бы весь API на время
конвертации (при `--workers 1` это недоступность системы, review/REVIEW.md §P1-2).
"""
from __future__ import annotations

import asyncio
import io
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.enums import PdfStatus
from app.services import storage as _storage_mod

logger = logging.getLogger("schoolhub.document_pdf")

PDF_MIME = "application/pdf"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC_MIME = "application/msword"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLS_MIME = "application/vnd.ms-excel"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PPT_MIME = "application/vnd.ms-powerpoint"
ODT_MIME = "application/vnd.oasis.opendocument.text"
ODS_MIME = "application/vnd.oasis.opendocument.spreadsheet"
RTF_MIME = "application/rtf"

#: Текстовые документы: упаковываем в PDF через ReportLab (без LibreOffice).
TEXT_MIMES = ("text/plain", "text/csv", "text/markdown")

#: Изображения: кладём на страницу A4 с сохранением пропорций.
IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")

#: Офисные документы, которые умеет LibreOffice. Значение — имя фильтра экспорта:
#: без явного фильтра LibreOffice выбирает модуль по расширению и может открыть
#: документ в Calc/Impress, что даёт пустой PDF.
LIBREOFFICE_FILTERS: dict[str, str] = {
    DOCX_MIME: "pdf:writer_pdf_Export",
    DOC_MIME: "pdf:writer_pdf_Export",
    RTF_MIME: "pdf:writer_pdf_Export",
    ODT_MIME: "pdf:writer_pdf_Export",
    XLSX_MIME: "pdf:calc_pdf_Export",
    XLS_MIME: "pdf:calc_pdf_Export",
    ODS_MIME: "pdf:calc_pdf_Export",
    PPTX_MIME: "pdf:impress_pdf_Export",
    PPT_MIME: "pdf:impress_pdf_Export",
}

SOFFICE_CANDIDATES = ("/usr/bin/soffice", "/usr/bin/libreoffice", "soffice", "libreoffice")

_SOFFICE_FLAGS = (
    "--headless",
    "--norestore",            # не предлагать восстановление после сбоя
    "--nologo",
    "--nofirststartwizard",
    "--nodefault",
    "--nolockcheck",          # не блокировать файл (важно при параллельных конвертациях)
)

#: Ограничение на одну конвертацию, секунд (ENV DOC_CONVERT_TIMEOUT_SEC).
def _timeout() -> int:
    from app.config import settings

    return int(getattr(settings, "doc_convert_timeout_sec", 60) or 60)


def soffice() -> str | None:
    """Путь к конвертеру LibreOffice или None, если он не установлен в образе."""
    for candidate in SOFFICE_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return None


# ------------------------------------------------------------------- планирование
@dataclass(frozen=True, slots=True)
class PdfPlan:
    """Что делать с файлом: способ конвертации и человекочитаемое пояснение."""

    method: str          # none | copy | libreoffice | image | text
    status: PdfStatus    # итоговый статус, если конвертация невозможна
    note: str | None = None


def plan_for(mime_type: str) -> PdfPlan:
    """План по MIME-типу. Вызывается ДО загрузки, чтобы сразу выставить pdf_status."""
    mime = (mime_type or "").lower().split(";")[0].strip()
    if mime == PDF_MIME:
        # оригинал уже PDF: копия не нужна, просмотр идёт по storage_path
        return PdfPlan("none", PdfStatus.NA, "Документ уже в формате PDF")
    if mime in IMAGE_MIMES:
        return PdfPlan("image", PdfStatus.PENDING)
    if mime in TEXT_MIMES:
        return PdfPlan("text", PdfStatus.PENDING)
    if mime in LIBREOFFICE_FILTERS:
        return PdfPlan("libreoffice", PdfStatus.PENDING)
    return PdfPlan("none", PdfStatus.NA, "Формат не конвертируется — файл открывается как есть")


# ------------------------------------------------------------------- конвертеры
def _font() -> tuple[str, str]:
    """Регистрирует кириллический шрифт для ReportLab, возвращает (regular, bold).

    Шрифты лежат в репозитории (backend/assets/fonts, Noto Sans, SIL OFL 1.1) —
    в отличие от встроенных Helvetica/Times они содержат кириллицу. Если каталога
    нет (например, образ собран без assets), откатываемся на DejaVu, который
    ставится пакетом `fonts-dejavu-core` в Dockerfile.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    assets = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
    candidates = (
        (assets / "NotoSans-Regular.ttf", assets / "NotoSans-Bold.ttf"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    )
    for regular, bold in candidates:
        if regular.exists():
            try:
                pdfmetrics.registerFont(TTFont("DocPdf", str(regular)))
                if bold.exists():
                    pdfmetrics.registerFont(TTFont("DocPdf-Bold", str(bold)))
                    pdfmetrics.registerFontFamily("DocPdf", normal="DocPdf", bold="DocPdf-Bold")
                    return "DocPdf", "DocPdf-Bold"
                return "DocPdf", "DocPdf"
            except Exception:  # noqa: BLE001 — шрифт не критичен, упадём в Helvetica
                logger.warning("Не удалось зарегистрировать шрифт %s", regular)
    return "Helvetica", "Helvetica-Bold"


def _image_to_pdf(src: Path) -> bytes:
    """Фотография/скан → PDF на странице A4 с полями 10 мм (масштаб по размеру)."""
    from PIL import Image  # noqa: F401  — проверка доступности до импорта reportlab
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    page_w, page_h = A4
    margin = 28.0                      # ~10 мм
    box_w, box_h = page_w - 2 * margin, page_h - 2 * margin
    c = canvas.Canvas(buf, pagesize=A4)
    img = ImageReader(str(src))
    iw, ih = img.getSize()
    scale = min(box_w / iw, box_h / ih)
    # увеличивать не запрещаем: скан 800×600 на весь лист читается лучше
    w, h = iw * scale, ih * scale
    c.drawImage(img, (page_w - w) / 2, (page_h - h) / 2, width=w, height=h,
                preserveAspectRatio=True, anchor="c")
    c.showPage()
    c.save()
    return buf.getvalue()


def _text_to_pdf(src: Path, original_name: str) -> bytes:
    """Текст/CSV → PDF. Кодировка угадывается (utf-8 → cp1251), строки не теряются."""
    raw = src.read_bytes()
    text_value = None
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "koi8-r"):
        try:
            text_value = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text_value is None:
        text_value = raw.decode("utf-8", errors="replace")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    regular, bold = _font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=15 * mm,
                            topMargin=16 * mm, bottomMargin=15 * mm, title=original_name)
    title_style = ParagraphStyle("t", fontName=bold, fontSize=11, leading=14, spaceAfter=6)
    body_style = ParagraphStyle("b", fontName=regular, fontSize=9, leading=12)
    story = [Paragraph(_xml_escape(original_name), title_style), Spacer(1, 4)]
    for line in text_value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        # пустая строка в Paragraph схлопнулась бы в ноль — показываем её пробелом
        story.append(Paragraph(_xml_escape(line) or "&nbsp;", body_style))
    doc.build(story)
    return buf.getvalue()


def _xml_escape(value: str) -> str:
    """Экранирование для Paragraph: ReportLab разбирает markup-теги внутри текста."""
    from xml.sax.saxutils import escape

    return escape(value or "")


def _libreoffice_to_pdf(src: Path, export_filter: str) -> bytes | None:
    """Конвертация офисного документа через LibreOffice headless. None — не вышло."""
    binary = soffice()
    if binary is None:
        return None
    with tempfile.TemporaryDirectory() as tmp_out, tempfile.TemporaryDirectory() as profile:
        try:
            proc = subprocess.run(
                # отдельный профиль на каждый запуск: параллельные конвертации не конфликтуют
                [binary, f"-env:UserInstallation=file://{profile}", *_SOFFICE_FLAGS,
                 "--convert-to", export_filter, "--outdir", tmp_out, str(src)],
                capture_output=True,
                timeout=_timeout(),
            )
        except subprocess.TimeoutExpired:
            logger.warning("Конвертация %s превысила %s c", src.name, _timeout())
            return None
        except OSError as exc:  # pragma: no cover - soffice пропал между which() и запуском
            logger.warning("Не удалось запустить LibreOffice: %r", exc)
            return None
        if proc.returncode != 0:
            logger.warning("LibreOffice вернул %s: %s", proc.returncode,
                           (proc.stderr or b"")[:300].decode("utf-8", "replace"))
            return None
        produced = sorted(Path(tmp_out).glob("*.pdf"))
        if not produced:
            return None
        return produced[0].read_bytes()


# ------------------------------------------------------------------- сборка
@dataclass(slots=True)
class PdfResult:
    """Итог конвертации: путь копии в хранилище, размер, статус и пояснение."""

    status: PdfStatus
    storage_path: str | None = None
    size_bytes: int | None = None
    note: str | None = None


def build_pdf(storage_path: str, mime_type: str, original_name: str) -> PdfResult:
    """СИНХРОННАЯ сборка PDF-копии. Из async-кода — только через `ensure_pdf()`.

    Возвращает PdfResult, а не бросает исключение: сбой конвертера не должен
    ронять загрузку документа. Оригиналом пользоваться можно всегда.
    """
    plan = plan_for(mime_type)
    if plan.method == "none":
        return PdfResult(plan.status, None, None, plan.note)

    try:
        src = _storage_mod.storage.abspath(storage_path)
    except Exception as exc:  # noqa: BLE001 — файла уже нет
        return PdfResult(PdfStatus.FAILED, None, None, f"Оригинал недоступен: {exc}"[:300])

    data: bytes | None = None
    note: str | None = None
    try:
        if plan.method == "image":
            data = _image_to_pdf(src)
        elif plan.method == "text":
            data = _text_to_pdf(src, original_name)
        elif plan.method == "libreoffice":
            data = _libreoffice_to_pdf(src, LIBREOFFICE_FILTERS[(mime_type or "").lower()])
            if data is None:
                note = ("В образе нет LibreOffice — доступен только оригинал"
                        if soffice() is None
                        else "Конвертер не справился с документом (таймаут или повреждённый файл)")
    except ImportError:
        # ReportLab/Pillow входят в requirements, но образ мог быть собран без них
        data = None
        note = "В образе нет библиотеки генерации PDF"
    except Exception as exc:  # noqa: BLE001 — недоверенный документ не должен ронять API
        logger.exception("Сбой конвертации %s", storage_path)
        data = None
        note = f"Ошибка конвертации: {exc}"[:300]

    if not data:
        return PdfResult(PdfStatus.FAILED, None, None, note)

    try:
        stored = _storage_mod.storage.save_bytes(data, f"{Path(original_name).stem}.pdf", PDF_MIME)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось сохранить PDF-копию %s", storage_path)
        return PdfResult(PdfStatus.FAILED, None, None, f"Не удалось сохранить копию: {exc}"[:300])
    return PdfResult(PdfStatus.READY, stored.storage_path, stored.size_bytes, None)


async def build_pdf_async(storage_path: str, mime_type: str, original_name: str) -> PdfResult:
    """То же, что `build_pdf()`, но в отдельном потоке — event loop не блокируется."""
    return await asyncio.to_thread(build_pdf, storage_path, mime_type, original_name)


async def ensure_pdf(row, *, force: bool = False) -> None:
    """Готовит PDF-копию для записи вложения и проставляет её поля.

    `row` — любой объект с `storage_path`, `mime_type`, `original_name`,
    `pdf_path`, `pdf_size`, `pdf_status`, `pdf_note` (ServiceNoteFile,
    HelpdeskFile, HelpArticleFile, TaskFile):duck typing вместо наследования,
    потому что таблицы исторически разные, а логика одна.

    Повторный вызов ничего не делает, пока копия READY или формат помечен NA —
    поэтому его безопасно дёргать и из фонового задания, и при первом просмотре.
    `force=True` пересоздаёт копию (например, после установки LibreOffice).
    """
    current = getattr(row, "pdf_status", None)
    current_value = current.value if isinstance(current, PdfStatus) else (current or "")
    if not force and current_value in (PdfStatus.READY.value, PdfStatus.NA.value):
        return

    result = await build_pdf_async(row.storage_path, row.mime_type, row.original_name)
    # прежнюю копию убираем: иначе в хранилище остался бы файл-сирота
    old_pdf = getattr(row, "pdf_path", None)
    if old_pdf and old_pdf != row.storage_path and result.storage_path != old_pdf:
        try:
            _storage_mod.storage.delete(old_pdf)
        except Exception:  # noqa: BLE001 — сирота не критичен, покажем его в «Файлах школы»
            pass
    row.pdf_path = result.storage_path
    row.pdf_size = result.size_bytes
    row.pdf_status = result.status
    row.pdf_note = result.note


def ensure_pdf_background(background, row) -> None:
    """Ставит конвертацию в FastAPI BackgroundTasks (после ответа клиенту).

    BackgroundTasks выполняет coroutine в event loop, а `ensure_pdf` уносит
    тяжёлую часть в поток — поэтому загрузка документа не тормозит ответ.
    """
    background.add_task(ensure_pdf, row)


def resolve_view(row) -> dict:
    """Как отдавать файл при `inline=true`: путь, MIME, имя и «можно ли показать».

    Порядок:
      1. готова PDF-копия → отдаём её (браузер рендерит PDF нативно);
      2. оригинал сам является PDF или изображением → отдаём как есть;
      3. остальное → attachment с подсказкой, что инлайн-просмотр недоступен.
    """
    status = getattr(row, "pdf_status", None)
    status_value = status.value if isinstance(status, PdfStatus) else (status or "")
    pdf_path = getattr(row, "pdf_path", None)
    name = row.original_name
    if status_value == PdfStatus.READY.value and pdf_path:
        return {"preview": "inline", "mime": PDF_MIME, "path": pdf_path,
                "filename": name if name.lower().endswith(".pdf") else f"{name}.pdf"}
    mime = (row.mime_type or "").lower()
    if mime == PDF_MIME or mime in IMAGE_MIMES:
        return {"preview": "inline", "mime": mime, "path": row.storage_path, "filename": name}
    return {"preview": "download", "mime": mime or "application/octet-stream",
            "path": row.storage_path, "filename": name}


def pdf_label(row) -> str:
    """Подпись состояния PDF-копии для интерфейса (показывается рядом с файлом)."""
    status = getattr(row, "pdf_status", None)
    value = status.value if isinstance(status, PdfStatus) else (status or "")
    note = getattr(row, "pdf_note", None)
    if value == PdfStatus.READY.value:
        return "PDF-копия готова"
    if value == PdfStatus.PENDING.value:
        return "PDF-копия готовится"
    if value == PdfStatus.FAILED.value:
        return note or "PDF-копию сделать не удалось"
    return note or "Открывается как есть"

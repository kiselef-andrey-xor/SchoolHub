"""Работа с документами приказов: отдача файла, превью, (опц.) конвертация DOCX→PDF.

Политика:
  * PDF отдаётся как есть с Content-Disposition: inline → нативное превью в браузере.
  * DOCX отдаётся как attachment (браузеры не рендерят docx). Для инлайн-просмотра
    включите конвертер: LibreOffice headless (`soffice --headless --convert-to pdf`).
    Конвертация выполняется один раз при загрузке, результат кэшируется рядом с файлом.

ВАЖНО про блокировку event loop
------------------------------
`convert_to_pdf()` — синхронная и вызывает `subprocess.run(timeout=…)`. Вызванная
напрямую из `async def`-хендлера она **останавливает весь event loop** на время
конвертации: при `--workers 1` API не отвечает никому, включая `/health` и
healthcheck Docker'а (review/REVIEW.md §P1-2).

Поэтому из асинхронного кода нужно вызывать **`preview_info_async()`** — он уносит
конвертацию в поток через `asyncio.to_thread`. Синхронный `preview_info()` оставлен
для фоновых задач и скриптов, где event loop'а нет.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks

from app.services import storage as _storage_mod

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC_MIME = "application/msword"
OFFICE_MIMES = (DOCX_MIME, DOC_MIME)

SOFFICE_CANDIDATES = ("/usr/bin/soffice", "/usr/bin/libreoffice", "soffice")

# Явный фильтр экспорта: без него LibreOffice выбирает фильтр по расширению и может
# открыть файл другим модулем (Calc/Impress), что даёт пустой PDF.
_PDF_FILTER = "pdf:writer_pdf_Export"

# Флаги, отключающие интерактив: документ недоверенный, диалогов быть не должно.
_SOFFICE_FLAGS = (
    "--headless",
    "--norestore",        # не предлагать восстановление после сбоя
    "--nologo",           # без заставки
    "--nofirststartwizard",
    "--nodefault",        # не создавать пустой документ
    "--nolockcheck",      # не блокировать файл (важно при параллельных конвертациях)
)


def soffice_available() -> bool:
    for candidate in SOFFICE_CANDIDATES:
        if shutil.which(candidate):
            return True
    return False


def _timeout() -> int:
    """Лимит на одну конвертацию. Вынесен в настройки: на DS223 (2 ГБ RAM, ARMv8)
    холодный LibreOffice заметно медленнее, чем на x86, и дефолт приходится поднимать."""
    from app.config import settings

    return int(getattr(settings, "doc_convert_timeout_sec", 60) or 60)


def convert_to_pdf(storage_path: str, timeout: int | None = None) -> str | None:
    """Конвертирует DOCX/DOC в PDF, возвращает storage_path PDF или None, если конвертер недоступен.

    Требуется в образе: `apt-get install -y libreoffice-writer --no-install-recommends`.

    Функция СИНХРОННАЯ и блокирующая — из async-хендлеров вызывать только через
    `preview_info_async()` / `asyncio.to_thread`.

    Остаточный риск: LibreOffice разбирает недоверенный OOXML, у него есть история
    CVE на парсерах и макросах. Меры здесь — отключение интерактива, отдельный профиль
    на каждый запуск и таймаут. Полноценная изоляция (отдельный контейнер-конвертер
    без доступа к БД и хранилищу, `cap_drop: [ALL]`, `no-new-privileges`, seccomp)
    описана в docs/DEPLOY-NAS.md и в review/REVIEW.md §5.11.
    """
    src = _storage_mod.storage.abspath(storage_path)
    if src.suffix.lower() not in {".docx", ".doc"}:
        return None
    if not soffice_available():
        return None

    cache = src.with_suffix(".preview.pdf")
    if cache.exists():
        return _rel_preview_path(storage_path)

    soffice = next((c for c in SOFFICE_CANDIDATES if shutil.which(c)), None)
    if not soffice:  # pragma: no cover
        return None

    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as profile:
        try:
            proc = subprocess.run(
                # свой профиль на каждый запуск: параллельные конвертации не конфликтуют
                [soffice, f"-env:UserInstallation=file://{profile}", *_SOFFICE_FLAGS,
                 "--convert-to", _PDF_FILTER, "--outdir", tmp, str(src)],
                capture_output=True,
                timeout=timeout if timeout is not None else _timeout(),
            )
        except subprocess.TimeoutExpired:
            # конвертер завис на «тяжёлом» документе: отдаём оригинал на скачивание,
            # а не роняем запрос и не держим поток вечно
            return None
        except OSError:  # pragma: no cover - soffice пропал между which() и запуском
            return None
        if proc.returncode != 0:  # pragma: no cover
            return None
        produced = list(Path(tmp).glob("*.pdf"))
        if not produced:  # pragma: no cover
            return None
        shutil.copy2(produced[0], cache)

    return _rel_preview_path(storage_path)


def _rel_preview_path(storage_path: str) -> str:
    """Относительный путь кэша превью в том же виде, в каком хранится storage_path."""
    return str(Path(*Path(storage_path).with_suffix(".preview.pdf").parts))


def preview_info(mime_type: str, storage_path: str) -> dict:
    """Что отдать фронту: inline-превью (pdf) или только скачивание (docx).

    СИНХРОННАЯ версия — блокирует вызывающий поток на время конвертации.
    Из async-хендлеров используйте `preview_info_async()`.
    """
    if mime_type == PDF_MIME:
        return {"preview": "inline", "mime": PDF_MIME, "path": storage_path}
    converted = convert_to_pdf(storage_path) if mime_type in OFFICE_MIMES else None
    if converted:
        return {"preview": "inline", "mime": PDF_MIME, "path": converted, "converted": True}
    return {"preview": "download", "mime": mime_type, "path": storage_path}


async def preview_info_async(mime_type: str, storage_path: str) -> dict:
    """То же, что `preview_info()`, но конвертация выполняется в отдельном потоке.

    Обязательная точка входа для async-хендлеров: синхронный `subprocess.run`
    внутри coroutine остановил бы event loop на весь таймаут конвертации, а при
    `--workers 1` это означает недоступность всего API (review/REVIEW.md §P1-2).
    """
    if mime_type == PDF_MIME:
        return {"preview": "inline", "mime": PDF_MIME, "path": storage_path}
    if mime_type not in OFFICE_MIMES:
        return {"preview": "download", "mime": mime_type, "path": storage_path}

    converted = await asyncio.to_thread(convert_to_pdf, storage_path)
    if converted:
        return {"preview": "inline", "mime": PDF_MIME, "path": converted, "converted": True}
    return {"preview": "download", "mime": mime_type, "path": storage_path}


def schedule_pdf_conversion(background: BackgroundTasks, mime_type: str, storage_path: str) -> None:
    """Фоновая конвертация DOCX/DOC → PDF сразу после загрузки документа.

    Результат кэшируется рядом с файлом (.preview.pdf), поэтому инлайн-превью
    открывается мгновенно. Если LibreOffice в образе нет — тихо пропускаем:
    документ останется доступным для скачивания (как раньше).

    Подключайте при КАЖДОЙ загрузке офисного документа, иначе первое превью пойдёт
    по холодному пути и займёт весь таймаут конвертера. Сейчас подключено для
    приказов, вложений задач, аттестации и файлов инструкций.
    """
    if mime_type not in OFFICE_MIMES:
        return
    if not soffice_available():
        return
    # BackgroundTasks выполняет задачу в пуле потоков, если она синхронная, —
    # event loop не блокируется
    background.add_task(convert_to_pdf, storage_path)

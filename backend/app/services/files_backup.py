"""«Файлы школы»: сводка по файловому хранилищу, ZIP-архив со всеми файлами
и восстановление из такого архива (Профиль → «Резервное копирование» → «Файлы школы»).

Зачем: дамп БД (кнопка «Скачать резервную копию») НЕ содержит прикрепленных файлов
(документы приказов, вложения задач, аватары). Без этого блока восстановление БД
оставляло бы «битые ссылки» — записи в order_files/task_files/users.avatar, для
которых файла на диске нет.

Контракт архива:
  schoolhub-files-<stamp>.zip
  ├── manifest.json   — версия формата, список файлов: путь хранилища, имя в архиве,
  │                     исходное имя, размер, sha256;
  └── files/…         — сами файлы под исходными именами (коллизии имён разрешаются
                        суффиксом __2, __3 …).

Восстановление (идемпотентно):
  * файла нет на диске          -> копируем из архива («восстановлено»);
  * файл есть и sha256 совпадает -> не трогаем («уже на месте»);
  * файл есть, но отличается    -> заменяем версией из архива («заменено»);
  * записи в manifest.json нет  -> архив отклоняется целиком (400);
  * путь из манифеста вне хранилища (zip-slip), нет члена архива и т.п. ->
    отбрасывается поштучно, в отчёте попадает в «ошибки»;
  * файлы архива, не описанные в манифесте, -> «сироты»: не распаковываются.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.errors import bad_request
from app.models.certification import Certification
from app.models.help import HelpArticle, HelpArticleFile
from app.models.helpdesk import HelpdeskFile
from app.models.order import OrderFile
from app.models.service_note import ServiceNoteFile
from app.models.task import TaskFile
from app.models.user import User
from app.models.vshk import VshkItem
from app.services import storage as storage_module

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
FILES_DIR = "files"
# превью DOCX→PDF — производный кэш, в архив и в сводку не попадает
PREVIEW_SUFFIX = ".preview.pdf"
# кусок поточной распаковки: память не растёт вместе с размером восстанавливаемого файла
CHUNK_SIZE = 1024 * 1024
# недописанный файл поточного восстановления: живёт только внутри restore_from_zip()
RESTORE_TMP_SUFFIX = ".part"


# --------------------------------------------------------------------- helpers
def _root() -> Path:
    """Корень файлового хранилища (в тестах storage подменяется на tmp_path)."""
    root = getattr(storage_module.storage, "root", None)
    return Path(root or settings.upload_dir).resolve()


def _sha256_path(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_fileobj(fh) -> str:
    hasher = hashlib.sha256()
    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
        hasher.update(chunk)
    return hasher.hexdigest()


def _disk_files(root: Path) -> list[Path]:
    """Все файлы хранилища (кроме производных превью и служебных каталогов)."""
    out: list[Path] = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.endswith((PREVIEW_SUFFIX, RESTORE_TMP_SUFFIX)):
            # превью — производный кэш; .part — недописанный файл восстановления
            # (обрыв распаковки): ни то ни другое не «файл школы»
            continue
        # служебные каталоги рядом с uploads (архивы аудита и т.п.) не входят в root
        out.append(path)
    return out


def _safe_rel(storage_path: str) -> str | None:
    """None, если путь из манифеста выглядывает из хранилища (zip-slip)."""
    if not storage_path or storage_path.startswith(("/", "\\")):
        return None
    parts = PurePosixPath(storage_path.replace("\\", "/")).parts
    if not parts or any(p in ("..", ".") for p in parts):
        return None
    return "/".join(parts)


async def collect_references(session) -> list[dict[str, Any]]:
    """Все ссылки БД на файлы хранилища.

    Перечень ОБЯЗАН совпадать со списком моделей, у которых есть путь в хранилище
    (проверяет `tests/test_files_backup_refs.py`): иначе файл попадает в архив под
    служебным именем `<uuid>.pdf` вместо исходного, а в сводке «Файлы школы»
    числится «сиротой», хотя на него ссылается документ.
    """
    refs: list[dict[str, Any]] = []
    rows = (
        await session.execute(select(OrderFile.storage_path, OrderFile.original_name))
    ).all()
    refs += [{"storage_path": p, "kind": "order_file", "original_name": n} for p, n in rows]
    rows = (
        await session.execute(select(TaskFile.storage_path, TaskFile.original_name))
    ).all()
    refs += [{"storage_path": p, "kind": "task_file", "original_name": n} for p, n in rows]
    # модули 2–4 и справочники: служебные записки, инструкции, сканы аттестации, итоги ВШК
    rows = (
        await session.execute(select(ServiceNoteFile.storage_path, ServiceNoteFile.original_name))
    ).all()
    refs += [{"storage_path": p, "kind": "note_file", "original_name": n} for p, n in rows]
    # НАСЛЕДИЕ до v1.12: единственный файл инструкции в колонках help_articles.file_*.
    # Новый код их не пишет, но строки остались в боевых базах — архив обязан их знать.
    rows = (await session.execute(select(HelpArticle.file_path, HelpArticle.file_name))).all()
    refs += [
        {"storage_path": p, "kind": "help_file", "original_name": n}
        for p, n in rows
        if p
    ]
    # v1.12: несколько вложений у инструкции
    rows = (await session.execute(
        select(HelpArticleFile.storage_path, HelpArticleFile.original_name))).all()
    refs += [{"storage_path": p, "kind": "help_file", "original_name": n} for p, n in rows]
    # v1.12: Helpdesk — фото поломок, акты, счета
    rows = (await session.execute(
        select(HelpdeskFile.storage_path, HelpdeskFile.original_name))).all()
    refs += [{"storage_path": p, "kind": "helpdesk_file", "original_name": n} for p, n in rows]
    # v1.12: автоматические PDF-копии документов — это ОТДЕЛЬНЫЕ файлы хранилища
    # (свой uuid-путь, не `.preview.pdf` рядом с оригиналом). Без них копия
    # числилась бы в сводке «Файлы школы» сиротой и не попала бы в архив:
    # восстановление из ZIP вернуло бы базу, в которой pdf_path указывает в никуда.
    # Каждое обращение написано явно: tests/test_files_backup_refs.py ищет в
    # исходнике этой функции литерал `Model.column`, поэтому цикл по списку
    # моделей тест бы не увидел.
    for column, kind in (
        (ServiceNoteFile.pdf_path, "note_pdf"),
        (TaskFile.pdf_path, "task_pdf"),
        (HelpArticleFile.pdf_path, "help_pdf"),
        (HelpdeskFile.pdf_path, "helpdesk_pdf"),
    ):
        rows = (await session.execute(select(column, column.table.c.original_name))).all()
        refs += [
            {"storage_path": p, "kind": kind, "original_name": f"{Path(n or 'document')}.pdf"}
            for p, n in rows
            if p
        ]
    rows = (await session.execute(select(Certification.file, Certification.file_name))).all()
    refs += [
        {"storage_path": p, "kind": "certification_file", "original_name": n}
        for p, n in rows
        if p
    ]
    rows = (await session.execute(select(VshkItem.result_file, VshkItem.result_file_name))).all()
    refs += [
        {"storage_path": p, "kind": "vshk_file", "original_name": n}
        for p, n in rows
        if p
    ]
    rows = (await session.execute(select(User.avatar, User.full_name))).all()
    refs += [
        {
            "storage_path": p,
            "kind": "avatar",
            "original_name": f"avatar-{(n or 'user')}{Path(p).suffix}",
        }
        for p, n in rows
        if p
    ]
    return refs


# --------------------------------------------------------------------- summary
async def summary(session) -> dict[str, Any]:
    root = _root()
    refs = await collect_references(session)
    disk = _disk_files(root)
    disk_rel = {p.relative_to(root).as_posix(): p for p in disk}

    broken: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        rel = ref["storage_path"]
        if rel in seen:
            continue
        seen.add(rel)
        if rel not in disk_rel:
            broken.append(ref)

    orphans = [
        {"storage_path": rel, "size_bytes": p.stat().st_size}
        for rel, p in disk_rel.items()
        if rel not in seen
    ]
    return {
        "files_on_disk": len(disk),
        "bytes_on_disk": sum(p.stat().st_size for p in disk),
        "db_references": len(refs),
        "broken_links": broken,
        "orphan_files": orphans[:200],
        "orphan_files_total": len(orphans),
        "manifest_version": MANIFEST_VERSION,
    }


# --------------------------------------------------------------------- zip
def build_zip(refs: list[dict[str, Any]]) -> Path:
    """Собирает ZIP во временный файл. Вызывается в worker-потоке (asyncio.to_thread)."""
    root = _root()
    disk_rel = {p.relative_to(root).as_posix(): p for p in _disk_files(root)}
    names_by_path = {r["storage_path"]: r.get("original_name") for r in refs}

    used: set[str] = set()
    manifest_files: list[dict[str, Any]] = []

    def _zip_name(rel: str) -> str:
        original = names_by_path.get(rel) or Path(rel).name
        original = Path(original).name or Path(rel).name     # без каталогов клиента
        candidate = f"{FILES_DIR}/{original}"
        if candidate in used:                                # коллизия исходных имён
            stem, suffix = Path(original).stem, Path(original).suffix
            n = 2
            while f"{FILES_DIR}/{stem}__{n}{suffix}" in used:
                n += 1
            candidate = f"{FILES_DIR}/{stem}__{n}{suffix}"
        used.add(candidate)
        return candidate

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="schoolhub-files-")
    import os

    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for rel in sorted(disk_rel):
                path = disk_rel[rel]
                zip_name = _zip_name(rel)
                zf.write(path, zip_name)
                manifest_files.append(
                    {
                        "storage_path": rel,
                        "zip_name": zip_name,
                        "original_name": names_by_path.get(rel) or Path(rel).name,
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256_path(path),
                    }
                )
            manifest = {
                "version": MANIFEST_VERSION,
                "app": "schoolhub",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": manifest_files,
            }
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=1))
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return Path(tmp_path)


# --------------------------------------------------------------------- restore
def restore_from_zip(source: "bytes | str | Path") -> dict[str, Any]:
    """Распаковывает архив в хранилище. Вызывается в worker-потоке.

    `source` — путь к файлу архива (предпочтительно) или байты. Путь обязателен для
    больших архивов: `bytes` держит ВЕСЬ архив в RAM, а лимит памяти контейнера
    backend — 460 МБ (review/REVIEW.md §P1-3). API пишет загрузку во временный файл
    потоково и передаёт сюда путь.

    Возвращает отчёт: restored/replaced/already/orphans/errors (списки имён/путей).
    """
    import io

    root = _root()
    report: dict[str, Any] = {
        "restored": [],
        "replaced": [],
        "already": [],
        "orphans": [],
        "errors": [],
    }

    try:
        zf = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else str(source))
    except zipfile.BadZipFile as exc:
        raise bad_request("Файл не является ZIP-архивом") from exc

    with zf:
        names = set(zf.namelist())
        if MANIFEST_NAME not in names:
            raise bad_request(
                "В архиве нет manifest.json — это не архив файлов SchoolHub. "
                "Архив создаётся кнопкой «Скачать все файлы (ZIP)»",
            )
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            entries = manifest["files"]
            if not isinstance(entries, list):
                raise ValueError("files")
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            raise bad_request("manifest.json повреждён или не соответствует формату SchoolHub") from exc

        declared: set[str] = set()
        max_entry = settings.max_upload_mb * 1024 * 1024 * 4   # файл хранилища ≤ 4×лимита загрузки
        # Per-entry лимита недостаточно: архив из тысяч членов по ~100 МБ пройдёт
        # поштучные проверки и займёт весь том. Ограничиваем СУММАРНЫЙ объём
        # распаковки и сверяемся со свободным местом на диске.
        max_total = settings.files_archive_unpacked_max_mb * 1024 * 1024
        total_written = 0
        try:
            free_bytes = shutil.disk_usage(root).free
        except OSError:  # pragma: no cover - файловая система без statvfs
            free_bytes = max_total
        # запас 256 МБ: том общий с БД и логами, забивать его до нуля нельзя
        free_budget = max(0, free_bytes - 256 * 1024 * 1024)
        for entry in entries:
            if not isinstance(entry, dict):
                report["errors"].append({"item": "?", "reason": "запись манифеста не объект"})
                continue
            rel = _safe_rel(str(entry.get("storage_path") or ""))
            zip_name = str(entry.get("zip_name") or "")
            if rel is None:
                if zip_name in names:      # член архива не должен всплыть «сиротой»
                    declared.add(zip_name)
                report["errors"].append(
                    {"item": zip_name or str(entry.get("storage_path")), "reason": "путь вне хранилища (zip-slip) — отброшен"}
                )
                continue
            if zip_name not in names:
                report["errors"].append({"item": rel, "reason": "в архиве нет члена " + zip_name})
                continue
            declared.add(zip_name)
            target = (root / rel).resolve()
            if not target.is_relative_to(root):
                report["errors"].append({"item": rel, "reason": "путь вне хранилища (zip-slip) — отброшен"})
                continue

            info = zf.getinfo(zip_name)
            if info.file_size > max_entry:
                report["errors"].append({"item": rel, "reason": "файл больше лимита"})
                continue
            # Суммарный объём и свободное место проверяем до распаковки — по размеру
            # из оглавления архива (zipfile не выдаёт из члена больше, чем заявлено).
            if total_written + info.file_size > max_total:
                report["errors"].append({
                    "item": rel,
                    "reason": f"превышен суммарный объём распаковки "
                              f"({settings.files_archive_unpacked_max_mb} МБ) — остальные файлы пропущены",
                })
                break
            if total_written + info.file_size > free_budget:
                report["errors"].append({
                    "item": rel,
                    "reason": "недостаточно свободного места на томе хранилища — восстановление прервано",
                })
                break

            # Поточная распаковка: член архива НЕ читается в память целиком.
            # Прежняя схема держала весь файл в RAM, а архив школы (сканы приказов,
            # видео ВШК) легко превышает лимит памяти контейнера (review §P1-3).
            # Пишем во временный файл рядом с целевым — тот же том, значит
            # os.replace() атомарен: читатели не увидят половину файла.
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + RESTORE_TMP_SUFFIX)
            digest = hashlib.sha256()
            written = 0
            too_big = False
            placed = False
            try:
                with zf.open(info) as src, tmp.open("wb") as dst:
                    while True:
                        chunk = src.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_entry:      # оглавление соврало — не доверяем ему
                            too_big = True
                            break
                        digest.update(chunk)
                        dst.write(chunk)
                if too_big:
                    report["errors"].append({"item": rel, "reason": "файл больше лимита"})
                    continue                          # finally уберёт .part
                hexdigest = digest.hexdigest()

                if target.exists():
                    if _sha256_path(target) == hexdigest:
                        report["already"].append(rel)
                        continue                      # содержимое совпало — файл не трогаем
                    verdict = "replaced"
                else:
                    verdict = "restored"
                os.replace(tmp, target)
                placed = True
                total_written += written
                # производное превью DOCX после замены должно быть пересоздано
                target.with_suffix(target.suffix + PREVIEW_SUFFIX).unlink(missing_ok=True)
                report[verdict].append(rel)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                # BadZipFile — битый CRC члена; RuntimeError — шифрованный член
                report["errors"].append({"item": rel, "reason": f"не удалось распаковать: {exc}"})
            finally:
                if not placed:
                    tmp.unlink(missing_ok=True)

        for name in sorted(names):
            if name in declared or name == MANIFEST_NAME:
                continue
            if name.endswith("/") or not name.startswith(FILES_DIR + "/"):
                report["errors"].append({"item": name, "reason": "вне папки files/ и не описан в манифесте"})
                continue
            report["orphans"].append(name)

    return report


def archive_dir() -> Path:
    """Каталог служебных архивов (рядом с хранилищем, но НЕ внутри uploads)."""
    return _root().parent / "audit_archive"


def write_audit_archive(rows: list[dict[str, Any]]) -> Path:
    """JSONL-файл с выгруженными из БД строками журнала (self-healing)."""
    dest = archive_dir()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = dest / f"audit-{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def cleanup_tmp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass


__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "archive_dir",
    "build_zip",
    "cleanup_tmp",
    "collect_references",
    "restore_from_zip",
    "summary",
    "write_audit_archive",
]

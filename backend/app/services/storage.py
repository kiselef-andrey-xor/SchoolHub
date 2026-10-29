"""Хранилище файлов.

По умолчанию — локальная ФС (content-addressed: <yy>/<mm>/<uuid>.<ext>), что
исключает коллизии имён и path traversal. Интерфейс FileStorage позволяет
без изменения бизнес-логики перейти на S3/MinIO (реализация-заглушка ниже).
"""
from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, NoReturn, Protocol

from app.config import settings
from app.errors import bad_request

_SAFE_EXT = re.compile(r"^[A-Za-z0-9]{1,10}$")


@dataclass(slots=True)
class StoredFile:
    storage_path: str        # относительный путь внутри upload_dir
    original_name: str
    mime_type: str
    size_bytes: int
    checksum: str


class FileStorage(Protocol):
    async def save(
        self, file: BinaryIO, original_name: str, mime_type: str, allowed_mimes: list[str] | None = None
    ) -> StoredFile: ...
    def save_bytes(self, data: bytes, original_name: str, mime_type: str) -> StoredFile: ...
    def duplicate(self, storage_path: str) -> StoredFile: ...
    def open(self, storage_path: str) -> BinaryIO: ...
    def abspath(self, storage_path: str) -> Path: ...
    def delete(self, storage_path: str) -> None: ...


class LocalFileStorage:
    """Локальная ФС. Путь формируется сервером, имя файла клиента используется только для отображения."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.upload_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ api
    async def save(
        self, file: BinaryIO, original_name: str, mime_type: str, allowed_mimes: list[str] | None = None
    ) -> StoredFile:
        self._validate_mime(mime_type, allowed_mimes)

        now = datetime.now(timezone.utc)
        ext = Path(original_name).suffix.lstrip(".").lower() or "bin"
        if not _SAFE_EXT.match(ext):
            raise bad_request("Некорректное расширение файла")

        rel_dir = Path(f"{now:%Y}/{now:%m}")
        abs_dir = self.root / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)

        file_id = uuid.uuid4()
        rel_path = rel_dir / f"{file_id}.{ext}"
        abs_path = self.root / rel_path

        hasher = hashlib.sha256()
        size = 0
        header = b""
        with abs_path.open("wb") as dst:
            while chunk := file.read(1024 * 1024):
                if size == 0:
                    header = chunk[:1024]
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    dst.close()
                    abs_path.unlink(missing_ok=True)
                    raise bad_request(f"Файл больше {settings.max_upload_mb} МБ")
                hasher.update(chunk)
                dst.write(chunk)

        if size == 0:
            abs_path.unlink(missing_ok=True)
            raise bad_request("Пустой файл")

        # заявленный клиентом MIME ничего не гарантирует — сверяем содержимое с сигнатурой
        self._validate_signature(mime_type, header, abs_path)

        return StoredFile(
            storage_path=str(rel_path),
            original_name=Path(original_name).name[:512],
            mime_type=mime_type,
            size_bytes=size,
            checksum=hasher.hexdigest(),
        )

    # ------------------------------------------------- серверная запись байтов
    def save_bytes(self, data: bytes, original_name: str, mime_type: str) -> StoredFile:
        """Сохраняет байты, ПОЛУЧЕННЫЕ НА СЕРВЕРЕ (PDF-копия, сгенерированный файл).

        Отдельный метод от `save()`: содержимое доверенное — его создал сам сервер,
        а не прислал клиент, поэтому проверка по списку разрешённых MIME и по
        сигнатуре не нужна. Путь формируется тем же content-addressed способом
        (год/месяц/uuid.ext), контрольная сумма считается, лимит `max_upload_bytes`
        не применяется: размер копии ограничен размером уже принятого оригинала.
        """
        if not data:
            raise bad_request("Пустой файл")
        now = datetime.now(timezone.utc)
        ext = Path(original_name).suffix.lstrip(".").lower() or "bin"
        if not _SAFE_EXT.match(ext):
            ext = "bin"
        rel_dir = Path(f"{now:%Y}/{now:%m}")
        (self.root / rel_dir).mkdir(parents=True, exist_ok=True)
        rel_path = rel_dir / f"{uuid.uuid4()}.{ext}"
        (self.root / rel_path).write_bytes(data)
        return StoredFile(
            storage_path=str(rel_path),
            original_name=Path(original_name).name[:512],
            mime_type=mime_type,
            size_bytes=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
        )

    def duplicate(self, storage_path: str, original_name: str | None = None,
                  mime_type: str = "application/octet-stream") -> StoredFile:
        """Физическая копия файла под НОВЫМ путём в том же хранилище.

        Зачем: вложения служебной записки передаются в задачу исполнителю (v1.12).
        Если просто сослаться на тот же `storage_path`, удаление записки или её
        вложения уничтожило бы документ у исполнителя — `delete()` unlink'ает файл,
        а на него ссылались бы две записи. Копия делает жизненные циклы независимыми.
        """
        src = self.abspath(storage_path)
        return self.save_bytes(src.read_bytes(), original_name or src.name, mime_type)

    def abspath(self, storage_path: str) -> Path:
        """Защита от path traversal: результирующий путь обязан лежать внутри root.

        is_relative_to (а не startswith) — иначе каталог-«сосед» вида
        /data/uploads_x ошибочно пройдёт проверку для root=/data/uploads.
        """
        candidate = (self.root / storage_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise bad_request("Некорректный путь к файлу")
        if not candidate.exists():
            raise bad_request("Файл отсутствует в хранилище")
        return candidate

    def open(self, storage_path: str) -> BinaryIO:
        return self.abspath(storage_path).open("rb")

    def delete(self, storage_path: str) -> None:
        try:
            candidate = (self.root / storage_path).resolve()
            if candidate.is_relative_to(self.root):
                candidate.unlink(missing_ok=True)
                # кэш инлайн-превью DOCX→PDF (.preview.pdf) удаляем вместе с файлом
                candidate.with_suffix(".preview.pdf").unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            pass

    def delete_pdf_pair(self, storage_path: str, pdf_path: str | None) -> None:
        """Удаляет оригинал и его автоматическую PDF-копию (v1.12).

        Копия лежит ОТДЕЛЬНЫМ файлом со своим uuid-именем (а не `.preview.pdf`
        рядом с оригиналом), поэтому `delete()` о ней не знает и оставил бы
        сироту в хранилище — она потом числилась бы в «Файлах школы» как
        orphan. Если pdf_path совпадает со storage_path (оригинал и был PDF),
        повторный unlink безвреден: `missing_ok=True`.
        """
        self.delete(storage_path)
        if pdf_path and pdf_path != storage_path:
            self.delete(pdf_path)

    # -------------------------------------------------------------- private
    # Сигнатуры форматов: content_type из заголовка запроса подделывается тривиально,
    # поэтому содержимое проверяется по magic bytes (и по структуре контейнера для OOXML).
    _TEXT_MIMES = {"text/plain", "text/csv"}
    _OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    _ZIP_SIGS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    _OOXML_MARKERS = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word/",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xl/",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ppt/",
    }

    def _validate_signature(self, mime_type: str, header: bytes, abs_path: Path) -> None:
        """Проверяет, что байты файла соответствуют заявленному MIME. Исключение → файл удаляется."""
        def fail() -> NoReturn:
            abs_path.unlink(missing_ok=True)
            raise bad_request(
                "Содержимое файла не соответствует заявленному типу — загрузка отклонена"
            )

        if mime_type in self._TEXT_MIMES:
            # тексты не имеют сигнатуры; как минимум не двоичные (NUL в первом килобайте)
            if b"\x00" in header:
                fail()
            return
        if mime_type == "application/pdf":
            # по спецификации %PDF- обязан встречаться в первых 1024 байтах
            if not header.startswith(b"%PDF-"):
                fail()
            return
        if mime_type in self._OOXML_MARKERS:
            if not header.startswith(self._ZIP_SIGS):
                fail()
            import zipfile

            marker = self._OOXML_MARKERS[mime_type]
            try:
                with zipfile.ZipFile(abs_path) as zf:
                    names = zf.namelist()
                if "[Content_Types].xml" not in names or not any(n.startswith(marker) for n in names):
                    fail()
            except zipfile.BadZipFile:
                fail()
            return
        if mime_type == "application/msword" or mime_type == "application/vnd.ms-excel":
            if not header.startswith(self._OLE2):
                fail()
            return
        if mime_type == "application/zip":
            if not header.startswith(self._ZIP_SIGS):
                fail()
            return
        if mime_type == "application/x-7z-compressed":
            if not header.startswith(b"7z\xbc\xaf\x27\x1c"):
                fail()
            return
        if mime_type == "application/x-rar-compressed":
            if not (header.startswith(b"Rar!\x1a\x07\x00") or header.startswith(b"Rar!\x1a\x07\x01\x00")):
                fail()
            return
        if mime_type == "image/jpeg":
            if not header.startswith(b"\xff\xd8\xff"):
                fail()
            return
        if mime_type == "image/png":
            if not header.startswith(b"\x89PNG\r\n\x1a\n"):
                fail()
            return
        if mime_type == "image/webp":
            if not (header.startswith(b"RIFF") and header[8:12] == b"WEBP"):
                fail()
            return

    @staticmethod
    def _validate_mime(mime_type: str, allowed_mimes: list[str] | None = None) -> None:
        allowed = allowed_mimes if allowed_mimes is not None else settings.allowed_doc_mimes
        if mime_type not in allowed:
            raise bad_request(
                "Недопустимый тип файла. Получен тип: " + (mime_type or "не определён")
            )


class S3FileStorage:  # pragma: no cover - заготовка под MinIO/S3
    """Точка расширения: boto3/aioboto3 + bucket policy. Интерфейс идентичен LocalFileStorage."""

    def __init__(self, bucket: str, endpoint_url: str) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url

    def __getattr__(self, item):
        raise NotImplementedError("S3FileStorage: подключите aioboto3 и реализуйте методы протокола FileStorage")


storage: FileStorage = LocalFileStorage()


def human_size(num: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "Б" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ТБ"


def copy_file(src: str, dst_root: Path) -> None:  # pragma: no cover - утилита для миграций
    shutil.copy2(storage.abspath(src), dst_root / Path(src).name)

# ============================================================================
#  SchoolHub backend. Мультиархитектурный базовый образ (amd64 + arm64),
#  поэтому сборка работает и на x86-сервере, и на Synology DS223 (Realtek ARMv8).
# ============================================================================
# trixie (Debian 13): в составе postgresql-client 17 — pg_dump НЕ СТАРШЕ сервера
# (в compose postgres:16). pg_dump младше сервера отказывается делать дамп,
# а книжный bookworm давал лишь pg_dump 15 → «резервная копия не работала».
FROM python:3.12-slim-trixie

# PYTHONUTF8/LANG — не «для красоты»: alembic.ini и часть конфигов читаются
# configparser'ом в кодировке ЛОКАЛИ (encoding="locale"), а в файлах есть кириллица.
# В ASCII-локали это UnicodeDecodeError, и поскольку AUTO_MIGRATE=true выполняет
# миграции в lifespan, контейнер просто не поднялся бы. Сегодня спасает PEP 538
# (locale coercion в C.UTF-8 у python:3.12-slim), но это случайная защита: она
# исчезнет при смене базового образа на Alpine или при PYTHONCOERCECLOCALE=0.
# См. review/REVIEW.md §5.2.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /code

# curl — для healthcheck; tzdata — корректные даты/логи в локальной зоне;
# postgresql-client — pg_dump для резервного копирования из интерфейса (Раздел → бекап).
# build-essential нужен только на этапе сборки wheel'ов (удаляем после установки).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata ca-certificates build-essential postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

# ---- инлайн-превью документов (конвертация в PDF через LibreOffice headless) ----
# Облегчённые nogui-пакеты (без GUI-библиотек). PDF-копия генерируется ФОНОВО сразу
# после загрузки документа и кэшируется рядом с файлом, поэтому на DS223 (2 ГБ RAM)
# конвертер нагружает систему только в момент загрузки.
#
# v1.12: пакетов три, а не один — PDF-копия создаётся для всех офисных форматов
# (app/services/document_pdf.py::LIBREOFFICE_FILTERS):
#   writer  — DOCX/DOC/ODT/RTF (filter writer_pdf_Export);
#   calc    — XLSX/XLS/ODS     (filter calc_pdf_Export);
#   impress — PPTX/PPT         (filter impress_pdf_Export).
# Без calc/impress LibreOffice отвечает «unknown filter», и файл помечался FAILED
# с причиной «нет конвертера», хотя writer в образе стоял.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
        fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# каталог загрузок и права непривилегированного пользователя
RUN mkdir -p /data/uploads /data/secrets \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /data /code
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# один воркер: экономия памяти (актуально для NAS с 2 ГБ RAM)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]

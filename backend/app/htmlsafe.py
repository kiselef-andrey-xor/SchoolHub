"""Безопасная сборка серверного HTML (печатные формы).

Правило модуля: **любое** значение, пришедшее из БД или из запроса, проходит через
`esc()` перед подстановкой в HTML. f-строка без `esc()` вокруг пользовательских
данных — это stored XSS (см. review/REVIEW.md §P0-2: так был сломан
`GET /notes/{id}/print`).

Почему не Jinja2: печатных форм в проекте мало, а автоэкранирование шаблонов
даёт ложное чувство безопасности — его отключают ровно тогда, когда нужно
вставить «готовый» фрагмент. Явный `esc()` на месте подстановки виден в диффе
и проверяется grep'ом:

    grep -n "HTMLResponse" -r app/api | ...   # каждая форма обязана использовать htmlsafe.page()

Что даёт `page()`:
  * `<!doctype html>` + `lang="ru"` + UTF-8;
  * `Content-Security-Policy` в <meta> — работает даже если nginx/backend не
    выставили заголовок (а для ответа, открытого напрямую по URL API, заголовок
    мог не дойти);
  * единый стиль печати, чтобы каждая форма не invent'ила его заново.
"""
from __future__ import annotations

from html import escape as _stdlib_escape

# Экранируем и кавычки: значения попадают в том числе в атрибуты.
esc = _stdlib_escape

# Печатная форма — статичный документ: ничего грузить извне не нужно.
# 'unsafe-inline' для style нужен, потому что стили объявлены в <style> внутри ответа;
# скрипты запрещены полностью (window.print() вызывается из отдельного файла print.js,
# который отдаётся со статики фронтенда, а не инлайном).
_PRINT_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "script-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

_BASE_STYLE = (
    "body{font-family:Georgia,'Times New Roman',serif;font-size:13pt;margin:32mm 20mm;color:#000}"
    ".head{display:flex;justify-content:space-between;gap:16mm}"
    ".visa{margin-top:16mm;text-align:right}"
    "h1{font-size:15pt;margin:8mm 0 4mm}"
    "p{margin:2mm 0}"
    "ul{margin:2mm 0 2mm 6mm}"
    "pre.body{white-space:pre-wrap;font-family:inherit;margin:4mm 0}"
    "table{border-collapse:collapse;width:100%}"
    "td,th{border:1px solid #999;padding:1.5mm 2mm;font-size:11pt;text-align:left}"
    "@media print{body{margin:15mm 12mm}}"
)


def page(title: str, body_html: str, *, extra_style: str = "", auto_print: bool = True,
         allow_script: bool | None = None) -> str:
    """Собирает полный HTML-документ печатной формы.

    `title` экранируется здесь; `body_html` — УЖЕ безопасная разметка, собранная
    вызывающим кодом через `esc()` для каждого пользовательского значения.

    `auto_print=True` (по умолчанию) выводит диалог печати открытием документа.
    Оба сценария требуют inline-обработчик, поэтому CSP точечно разрешает
    `'unsafe-inline'` для скриптов (см. `_csp()`). Внешние ресурсы запрещены
    всегда: украденный из тела записки `<img src=//evil>` не сработает, а
    `<script src=...>` не загрузится.

    `allow_script` (v1.12) разрешает inline-скрипт БЕЗ автопечати: форма
    служебной записки открывается в новой вкладке, чтобы её сначала прочитали,
    и печатается по кнопке в панели инструментов. По умолчанию значение
    наследуется от `auto_print`, поэтому прежние формы ведут себя как раньше.
    """
    onload = ' onload="window.print()"' if auto_print else ""
    scriptable = auto_print if allow_script is None else allow_script
    return (
        "<!doctype html>"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{esc(_csp(scriptable), quote=True)}">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{esc(title)}</title>"
        f"<style>{_BASE_STYLE}{extra_style}</style>"
        f"</head><body{onload}>{body_html}</body></html>"
    )


def _csp(scriptable: bool) -> str:
    """CSP печатной формы. Inline-обработчик (автопечать или кнопка «Печать»)
    требует точечного разрешения скриптов — внешние источники запрещены всегда."""
    if not scriptable:
        return _PRINT_CSP
    return _PRINT_CSP.replace("script-src 'none'", "script-src 'unsafe-inline'")

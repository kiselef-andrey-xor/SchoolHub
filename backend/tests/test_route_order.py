"""Порядок маршрутов в роутерах (review/REVIEW.md §6.5).

Реальный прецедент: `@router.get("/{note_id}")` стоял ВЫШЕ `@router.get("/export.csv")`,
и запрос `/notes/export.csv` попадал в карточку с `note_id="export.csv"` → 404 вместо
выгрузки CSV (исправлено в коммите 73382a7). FastAPI сопоставляет маршруты строго
в порядке объявления и НЕ «проваливается» дальше при ошибке валидации, поэтому
динамический сегмент `{...}` перехватывает любой статический путь, подходящий под
его регулярное выражение.

Тест проходит по всем `APIRouter` в `app/api/` и проверяет: для каждого статического
пути нет РАНЕЕ объявленного динамического маршрута, который его перехватывает.
"""

import importlib
import pkgutil

from fastapi import APIRouter
from fastapi.routing import APIRoute

import app.api as api_pkg


def _iter_routers():
    """Все APIRouter, объявленные в модулях пакета app.api (включая вложенные)."""
    seen: set[int] = set()
    for mod_info in pkgutil.walk_packages(api_pkg.__path__, prefix=api_pkg.__name__ + "."):
        try:
            module = importlib.import_module(mod_info.name)
        except Exception:  # pragma: no cover - модуль без импорта приложения не собрать
            raise
        for name, obj in vars(module).items():
            if isinstance(obj, APIRouter) and id(obj) not in seen:
                seen.add(id(obj))
                yield f"{module.__name__}.{name}", obj


def _routes(router: APIRouter) -> list[APIRoute]:
    return [r for r in router.routes if isinstance(r, APIRoute)]


def test_no_static_route_is_shadowed_by_earlier_dynamic_one():
    problems: list[str] = []

    for where, router in _iter_routers():
        routes = _routes(router)
        for idx, route in enumerate(routes):
            if "{" in route.path:
                continue  # динамический маршрут сам по себе никого не заслоняет
            for earlier in routes[:idx]:
                if "{" not in earlier.path:
                    continue
                match = earlier.path_regex.match(route.path)
                if match:
                    methods = sorted(route.methods or {})
                    earlier_methods = sorted(earlier.methods or {})
                    if set(methods) & set(earlier_methods):
                        problems.append(
                            f"{where}: {methods} {route.path} перехватывается более ранним "
                            f"{earlier_methods} {earlier.path} (объявлен выше по коду)"
                        )

    assert not problems, (
        "статические маршруты должны объявляться ДО динамических с тем же префиксом "
        "(иначе /export.csv уходит в /{id}):\n  " + "\n  ".join(problems)
    )


def test_routers_are_found_by_the_check():
    """Страховка от «тест ничего не проверяет»: роутеры действительно находятся."""
    found = dict(_iter_routers())
    assert len(found) >= 10, f"найдено подозрительно мало роутеров: {sorted(found)}"
    assert any(name.endswith("router") for name in found)
    # ключевые роутеры, в которых уже был дефект порядка, обязаны попасть в проверку
    assert any("service_notes" in name for name in found), sorted(found)


def test_no_duplicate_routes_in_one_router():
    """Два одинаковых (метод, путь) в одном роутере = мёртвый код.

    FastAPI оставит первый матч, второй обработчик недостижим — обычно это след
    merge-конфликта или копипасты. Отдельный от порядка дефект, ловится так же дёшево.
    """
    duplicates: list[str] = []
    for where, router in _iter_routers():
        seen: dict[tuple[str, frozenset[str]], str] = {}
        for route in _routes(router):
            key = (route.path, frozenset(route.methods or {}))
            if key in seen:
                duplicates.append(
                    f"{where}: {sorted(route.methods or {})} {route.path} объявлен дважды "
                    f"(второй обработчик недостижим)"
                )
            seen[key] = route.name

    assert not duplicates, "\n  ".join(duplicates)


def test_export_and_print_routes_reachable():
    """Выгрузки/печатные формы не должны быть перехвачены маршрутами вида /{id}.

    Это и есть прецедент 73382a7, только в разрезе «файловых» путей: проверяем
    пересечение по МЕТОДАМ (PATCH/DELETE на /{id} не мешают GET-выгрузке).
    """
    FILE_SUFFIXES = (".csv", ".pdf", ".xlsx", ".docx", ".zip")
    problems: list[str] = []

    for where, router in _iter_routers():
        routes = _routes(router)
        for idx, route in enumerate(routes):
            if not route.path.endswith(FILE_SUFFIXES):
                continue
            for earlier in routes[:idx]:
                if "{" not in earlier.path or not earlier.path_regex.match(route.path):
                    continue
                if set(route.methods or {}) & set(earlier.methods or {}):
                    problems.append(
                        f"{where}: {sorted(route.methods or {})} {route.path} недостижим — "
                        f"выше объявлен {sorted(earlier.methods or {})} {earlier.path}"
                    )

    assert not problems, (
        "перенесите выгрузку/печатную форму ВЫШЕ динамического маршрута:\n  "
        + "\n  ".join(problems)
    )

"""Обезличивание на путях вывода МИМО pydantic-схем (review/REVIEW.md §P1-1).

Pydantic-сериализаторы масок не спасают там, где ответ собирается вручную из
ORM-объектов: CSV-экспорты, печатные формы, текст журнала событий. Для них есть
две функции — `lockdown.disp()` (значение + seed) и `lockdown.mask_changes()`
(словарь снимка полей). Здесь они проверяются как чистые функции: без БД и HTTP,
чтобы регрессия ловилась мгновенно и с понятным местом падения.
"""
from __future__ import annotations

import uuid

import pytest

from app.services import lockdown


@pytest.fixture(autouse=True)
def _reset_masking():
    """Флаг маскирования живёт в contextvar: убираем за собой даже после падения теста."""
    yield
    lockdown.set_masking(False)


def _masked(on: bool):
    """Включает/выключает маскирование в рамках текущего контекста."""
    lockdown.set_masking(on)


# ------------------------------------------------------------------------- disp()
def test_disp_returns_real_value_without_lockdown():
    _masked(False)
    assert lockdown.disp("Иванов Иван Иванович", "seed-1") == "Иванов Иван Иванович"
    assert lockdown.disp(None, "seed-1") == ""
    assert lockdown.disp_or_blank("", "seed-1") == "—"


def test_disp_masks_by_kind_and_is_stable_per_seed():
    _masked(True)
    uid = uuid.uuid4()

    emp = lockdown.disp("Иванов Иван Иванович", uid)
    assert emp.startswith("Сотрудник ")
    # тот же человек — та же метка на всех экранах и во всех выгрузках
    assert emp == lockdown.disp("Иванов Иван Иванович", uid)
    # другой сотрудник — другая метка
    assert emp != lockdown.disp("Иванов Иван Иванович", uuid.uuid4())

    assert lockdown.disp("Сидоров Пётр", kind="student").startswith("Ученик ")
    assert lockdown.disp("Сидорова Мария", kind="guardian").startswith("Представитель ")

    # обучающийся/представитель — свободный текст без id: seed берётся из значения,
    # поэтому один и тот же ребёнок всегда получает один номер
    assert lockdown.disp("Сидоров Пётр", kind="student") == lockdown.disp("Сидоров Пётр", kind="student")

    _masked(False)
    assert lockdown.disp("Иванов Иван Иванович", uid) == "Иванов Иван Иванович"


def test_disp_never_leaks_the_original_value():
    """Метка не должна содержать ни имени, ни его фрагментов."""
    _masked(True)
    name = "Иванов-Коваленко Иван Иванович"
    out = lockdown.disp(name, name, kind="student")
    assert "Иванов" not in out and "Иван" not in out and "Коваленко" not in out
    _masked(False)


# ------------------------------------------------------------------ mask_changes()
def test_mask_changes_is_identity_without_lockdown():
    _masked(False)
    changes = {"full_name": {"from": "Иванов И. И.", "to": "Петров П. П."}, "phone": "+7 900 000-00-00"}
    assert lockdown.mask_changes(changes) == changes
    assert lockdown.mask_changes(None) is None
    assert lockdown.mask_changes({}) == {}


def test_mask_changes_handles_all_value_shapes():
    _masked(True)
    out = lockdown.mask_changes({
        "full_name": {"from": "Иванов Иван", "to": "Петров Пётр"},   # diff
        "signatory": "Директоров А. А.",                              # скаляр
        "student": "Сидоров Пётр",                                    # другой вид маски
        "guardian": "Сидорова Мария",
        "responsibles": ["Иванов И.", "Петров П."],                   # список
        "before": {"login": "ivanov", "position": "Учитель"},         # вложенный словарь
        "after": {"login": "petrov", "position": "Завуч"},
        "phone": "+7 900 123-45-67",                                  # контакт
        "email": "ivanov@school.ru",
        "position": "Учитель математики",                             # НЕ персональные
        "count": 3,
        "movement_access": True,
        "note": None,
    })

    assert out["full_name"]["from"].startswith("Сотрудник ")
    assert out["full_name"]["to"].startswith("Сотрудник ")
    assert out["full_name"]["from"] != out["full_name"]["to"]        # факт изменения виден
    assert out["signatory"].startswith("Сотрудник ")
    assert out["student"].startswith("Ученик ")
    assert out["guardian"].startswith("Представитель ")
    assert all(v.startswith("Сотрудник ") for v in out["responsibles"])
    assert out["before"]["login"].startswith("Сотрудник ")
    assert out["after"]["login"].startswith("Сотрудник ")

    # контакты закрываются заглушкой: метка «Сотрудник N» вместо телефона читалась бы как имя
    assert out["phone"] == lockdown.CONTACT_PLACEHOLDER
    assert out["email"] == lockdown.CONTACT_PLACEHOLDER

    # неперсональные поля остаются как есть — иначе журнал теряет смысл
    assert out["position"] == "Учитель математики"
    assert out["before"]["position"] == "Учитель"
    assert out["count"] == 3
    assert out["movement_access"] is True
    assert out["note"] is None

    _masked(False)


def test_mask_changes_removes_every_pii_key():
    """Регрессия: новый ключ с персональными данными не должен «проскакивать»."""
    _masked(True)
    pii = {key: "Иванов Иван Иванович" for key in (
        "full_name", "login", "login_hint", "domain_account", "employee", "actor_name",
        "signatory", "resp", "responsibles", "assignees", "user", "groups",
        "student", "guardian",
    )}
    out = lockdown.mask_changes(pii)
    for key, value in out.items():
        assert "Иванов" not in str(value), f"ключ {key} не обезличен"
    _masked(False)


# ------------------------------------------------------------------- mask_name_map()
def test_mask_name_map():
    names = {str(uuid.uuid4()): "Иванов Иван", str(uuid.uuid4()): "Петров Пётр"}
    _masked(False)
    assert lockdown.mask_name_map(names) == names
    assert lockdown.mask_name_map(None) is None

    _masked(True)
    out = lockdown.mask_name_map(names)
    assert set(out) == set(names)                     # ключи (id) не меняются
    assert all(v.startswith("Сотрудник ") for v in out.values())
    assert len(set(out.values())) == len(names)       # разным людям — разные метки
    _masked(False)

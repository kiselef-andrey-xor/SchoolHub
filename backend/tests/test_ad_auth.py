"""Аутентификация через Active Directory: bind, провижининг, whitelist групп, роли, троттлинг.

Реальный контроллер домена в тестах не нужен: `ADProvider` — интерфейс, поэтому
подменяем его заглушкой и проверяем всю бизнес-логику входа (включая ошибки AD-кодов).
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.errors import forbidden, unauthorized
from app.services import ad, ad_users
from app.services.ad import ADPrincipal, ADProvider
from tests.conftest import DEMO_PASSWORD, auth, login, new_client

pytestmark = pytest.mark.asyncio


class FakeAD(ADProvider):
    """Заглушка каталога: база «доменных» пользователей + имитация кодов ошибок AD."""

    def __init__(self, users: dict[str, ADPrincipal], passwords: dict[str, str]) -> None:
        self.users = users
        self.passwords = passwords
        self.disabled: set[str] = set()
        self.locked: set[str] = set()
        self.mode: str = "ok"          # ok | unavailable | bad_service
        self.bind_calls = 0
        self.search_calls = 0

    async def authenticate(self, username: str, password: str) -> ADPrincipal:
        if self.mode == "unavailable":
            raise ad.ADError("Контроллер домена недоступен: dc01.school.test:636")
        self.bind_calls += 1
        key = username.lower()
        if key in self.locked:
            raise forbidden("Учётная запись заблокирована из-за превышения числа попыток входа (код AD 775)")
        if key in self.disabled:
            raise forbidden("Учётная запись в домене отключена")
        if key not in self.users or self.passwords.get(key) != password:
            raise unauthorized("Неверное имя пользователя или пароль")
        return self.users[key]

    async def search_users(self, query: str, limit: int = 50) -> list[ADPrincipal]:
        if self.mode == "bad_service":
            raise ad.ADError("Сервисная учётка LDAP отклонена контроллером домена")
        self.search_calls += 1
        q = query.lower()
        return [
            p
            for p in self.users.values()
            if q in p.username.lower() or q in p.full_name.lower() or q in p.email.lower()
        ][:limit]

    async def ping(self) -> dict:
        if self.mode == "unavailable":
            return {"ok": False, "error": "timeout"}
        return {"ok": True, "server": "dc01.school.test:636"}


def make_principal(
    username: str,
    full_name: str,
    *,
    email: str | None = None,
    position: str = "",
    groups: list[str] | None = None,
) -> ADPrincipal:
    return ADPrincipal(
        username=username,
        dn=f"CN={full_name},OU=Users,DC=school,DC=test",
        upn=f"{username}@school.test",
        email=email or f"{username}@school.test",
        full_name=full_name,
        position=position,
        phone="+7 495 000-00-00",
        groups=groups or ["Users", "SchoolStaff"],
        group_dns=[f"CN={g},OU=Groups,DC=school,DC=test" for g in (groups or ["Users", "SchoolStaff"])],
    )


@pytest.fixture
def fake_ad(monkeypatch):
    """Включает «AD» и подставляет заглушку провайдера."""
    provider = FakeAD(
        users={
            "ivanov": make_principal("ivanov", "Иванов Пётр Сергеевич", position="Учитель математики"),
            "zavuch": make_principal(
                "zavuch", "Морозова Ольга Петровна", position="Завуч", groups=["Users", "SchoolAdmins"]
            ),
            "outsider": make_principal("outsider", "Посторонний Иван", groups=["Users"]),
        },
        passwords={"ivanov": "DomainPass1!", "zavuch": "DomainPass1!", "outsider": "DomainPass1!"},
    )

    monkeypatch.setattr(settings, "ldap_enabled", True)
    monkeypatch.setattr(settings, "ldap_server", "dc01.school.test:636")
    monkeypatch.setattr(settings, "ldap_base_dn", "DC=school,DC=test")
    monkeypatch.setattr(settings, "ldap_domain", "school.test")
    monkeypatch.setattr(settings, "ldap_bind_dn", "CN=svc_schoolhub,OU=Service,DC=school,DC=test")
    monkeypatch.setattr(settings, "ldap_bind_password", "svc-password")
    monkeypatch.setattr(settings, "ldap_allowed_groups", [])
    monkeypatch.setattr(settings, "ldap_admin_groups", [])
    monkeypatch.setattr(settings, "ldap_owner_groups", [])
    monkeypatch.setattr(settings, "ldap_provisioning", "auto")
    monkeypatch.setattr(settings, "ldap_default_role", ad_users.Role.TEACHER)
    monkeypatch.setattr(settings, "ldap_role_from_groups", "upgrade")
    monkeypatch.setattr(settings, "login_rate_limit", 100)   # не мешать прочим тестам

    ad.set_provider(provider)
    yield provider
    ad.set_provider(None)


# ------------------------------------------------------------------------ вход
async def test_ad_login_provisions_user(client, seeded, fake_ad):
    resp = await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    assert resp.status_code == 200, resp.text
    tokens = resp.json()
    assert tokens["access_token"]
    assert resp.cookies.get("schoolhub_refresh"), "refresh-cookie выдаётся и при доменном входе"

    c = new_client(client)
    c.headers["Authorization"] = f"Bearer {tokens['access_token']}"
    me = (await c.get("/auth/me")).json()

    assert me["email"] == "ivanov@school.test"
    assert me["full_name"] == "Иванов Пётр Сергеевич"
    assert me["positions"][0]["title"] == "Учитель математики"   # подтянуто из AD (title)
    assert me["role"] == "TEACHER"                          # роль по умолчанию
    assert me["auth_source"] == "ldap"
    assert me["domain_account"] == "ivanov"
    assert set(me["ad_groups"]) == {"Users", "SchoolStaff"}
    # v1.11: замещения — только администрация, учителям capability не выдаётся.
    # v1.12: helpdesk:read есть у всех ролей — заявку в техподдержку подаёт любой сотрудник.
    assert me["capabilities"] == [
        "orders:read", "orders:ack", "tasks:read-own", "plans:read",
        "certifications:read", "notes:read", "helpdesk:read",
    ]


@pytest.mark.parametrize("login_value", ["zavuch", "zavuch@school.test", "SCHOOL\\zavuch", "ZAVUCH"])
async def test_ad_login_accepts_different_login_formats(client, seeded, fake_ad, login_value):
    resp = await client.post("/auth/login", json={"email": login_value, "password": "DomainPass1!"})
    assert resp.status_code == 200, f"{login_value}: {resp.text}"


async def test_ad_login_rejects_wrong_password(client, seeded, fake_ad):
    resp = await client.post("/auth/login", json={"email": "ivanov", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"
    assert fake_ad.bind_calls == 1


async def test_ad_login_unknown_user_is_401_not_500(client, seeded, fake_ad):
    resp = await client.post("/auth/login", json={"email": "no-such-user", "password": "DomainPass1!"})
    assert resp.status_code == 401
    # формулировка не раскрывает, существует ли учётка в домене
    same = await client.post("/auth/login", json={"email": "ivanov", "password": "wrong-password"})
    assert same.json()["detail"] == resp.json()["detail"]


async def test_ad_unavailable_returns_503(client, seeded, fake_ad):
    fake_ad.mode = "unavailable"
    resp = await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    assert resp.status_code == 503
    assert resp.json()["code"] == "AD_ERROR"
    assert "недоступен" in resp.json()["detail"].lower()


async def test_locked_ad_account_returns_403(client, seeded, fake_ad):
    fake_ad.locked.add("ivanov")
    resp = await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    assert resp.status_code == 403
    assert "775" in resp.json()["detail"]


# ------------------------------------------------------- группы и роли
async def test_group_whitelist_blocks_outsider(client, seeded, fake_ad, monkeypatch):
    monkeypatch.setattr(settings, "ldap_allowed_groups", ["SchoolStaff"])

    ok = await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    assert ok.status_code == 200

    denied = await client.post("/auth/login", json={"email": "outsider", "password": "DomainPass1!"})
    assert denied.status_code == 403
    assert "сотрудникам школы" in denied.json()["detail"]

    # учётка в SchoolHub НЕ создана
    owner = await auth(new_client(client), "owner@example.com")
    users = (await owner.get("/users", params={"per_page": 100})).json()["items"]
    assert "outsider@school.test" not in {u["email"] for u in users}


async def test_admin_group_promotes_role_on_first_login(client, seeded, fake_ad, monkeypatch):
    monkeypatch.setattr(settings, "ldap_admin_groups", ["SchoolAdmins"])

    resp = await client.post("/auth/login", json={"email": "zavuch", "password": "DomainPass1!"})
    assert resp.status_code == 200
    c = new_client(client)
    c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    me = (await c.get("/auth/me")).json()
    assert me["role"] == "ADMIN"
    assert "orders:manage" in me["capabilities"]


async def test_existing_role_is_never_downgraded_by_groups(client, seeded, fake_ad, monkeypatch):
    """Исключение из группы AD не должно молча лишать доступа — понижение только вручную."""
    monkeypatch.setattr(settings, "ldap_admin_groups", ["SchoolAdmins"])

    await client.post("/auth/login", json={"email": "zavuch", "password": "DomainPass1!"})
    owner = await auth(new_client(client), "owner@example.com")
    users = (await owner.get("/users", params={"search": "zavuch"})).json()["items"]
    uid = users[0]["id"]
    assert users[0]["role"] == "ADMIN"

    # «перевели в обычные учителя»: группы больше нет
    fake_ad.users["zavuch"].groups = ["Users"]
    fake_ad.users["zavuch"].group_dns = ["CN=Users,OU=Groups,DC=school,DC=test"]

    resp2 = await client.post("/auth/login", json={"email": "zavuch", "password": "DomainPass1!"})
    assert resp2.status_code == 200
    after = (await owner.get(f"/users/{uid}")).json()
    assert after["role"] == "ADMIN", "роль не должна понижаться автоматически"
    assert after["ad_groups"] == ["Users"], "состав групп при этом синхронизируется"


async def test_manual_provisioning_blocks_auto_creation(client, seeded, fake_ad, monkeypatch):
    monkeypatch.setattr(settings, "ldap_provisioning", "manual")
    resp = await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    assert resp.status_code == 403
    assert "LDAP_PROVISIONING=manual" in resp.json()["detail"]


# ------------------------------------------------- локальные учётки при AD
async def test_local_break_glass_account_still_works(client, seeded, fake_ad):
    """Локальный Владелец — запасной вход, если DC недоступен."""
    before = fake_ad.bind_calls
    c = await login(new_client(client), "owner@example.com", DEMO_PASSWORD)
    me = (await c.get("/auth/me")).json()
    assert me["role"] == "OWNER"
    assert me["auth_source"] == "local"
    assert fake_ad.bind_calls == before, "для локальной учётки в каталог ходить нельзя"


async def test_ldap_user_cannot_change_password_in_app(client, seeded, fake_ad):
    resp = await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    c = new_client(client)
    c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    change = await c.post(
        "/auth/change-password", json={"current_password": "DomainPass1!", "new_password": "NewPass123!"}
    )
    assert change.status_code == 403
    assert "Active Directory" in change.json()["detail"]


async def test_admin_cannot_set_password_for_ldap_account(client, seeded, fake_ad):
    await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    o = await auth(client, "owner@example.com")
    users = (await o.get("/users", params={"search": "ivanov"})).json()["items"]
    uid = users[0]["id"]

    resp = await o.patch(f"/users/{uid}", json={"password": "Whatever123!"})
    assert resp.status_code == 409

    # а роль поменять можно — этим и занимается Владелец
    resp = await o.patch(f"/users/{uid}", json={"role": "ADMIN"})
    assert resp.status_code == 200 and resp.json()["role"] == "ADMIN"


# ------------------------------------------------------------ троттлинг входа
async def test_login_rate_limit_protects_ad_accounts(client, seeded, fake_ad, monkeypatch):
    from app.services.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter(max_attempts=3, window_sec=60)
    monkeypatch.setattr("app.api.auth.login_limiter", limiter)

    for _ in range(3):
        resp = await client.post("/auth/login", json={"email": "ivanov", "password": "wrong"})
        assert resp.status_code == 401

    blocked = await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"
    assert limiter.stats()["tracked_accounts"] >= 1


# ------------------------------------------------------- импорт из AD (admin)
async def test_ad_search_and_import(client, seeded, fake_ad):
    o = await auth(client, "owner@example.com")
    found = await o.get("/users/ad/search", params={"q": "Морозова"})
    assert found.status_code == 200
    assert any(u["username"] == "zavuch" for u in found.json())

    imported = await o.post(
        "/users/ad/import", json={"usernames": ["zavuch", "ivanov", "zavuch"], "role": "ADMIN"}
    )
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["total"] == 2
    assert sorted(body["created"]) == ["ivanov@school.test", "zavuch@school.test"]
    assert body["skipped"] == []

    # повторный импорт — идемпотентен
    again = await o.post("/users/ad/import", json={"usernames": ["zavuch"]})
    assert again.json()["skipped"] == ["zavuch"] and again.json()["total"] == 0

    # импортированный входит через AD и сохраняет назначенную роль
    resp = await client.post("/auth/login", json={"email": "zavuch", "password": "DomainPass1!"})
    assert resp.status_code == 200
    c = new_client(client)
    c.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    me = (await c.get("/auth/me")).json()
    assert me["role"] == "ADMIN" and me["auth_source"] == "ldap"


async def test_ad_import_forbidden_for_teacher_and_admin_role_escalation(client, seeded, fake_ad):
    t = await auth(client, "teacher1@example.com")
    assert (await t.get("/users/ad/search", params={"q": "ivanov"})).status_code == 403

    a = await auth(client, "admin@example.com")
    found = await a.get("/users/ad/search", params={"q": "ivanov"})
    assert found.status_code == 200

    # администрация не может импортировать с ролью ADMIN/OWNER (анти-эскалация)
    resp = await a.post("/users/ad/import", json={"usernames": ["ivanov"], "role": "OWNER"})
    assert resp.status_code == 403
    resp = await a.post("/users/ad/import", json={"usernames": ["ivanov"], "role": "TEACHER"})
    assert resp.status_code == 200


# ------------------------------------------------------------------ диагностика
async def test_ad_diagnostics_endpoint(client, seeded, fake_ad):
    o = await auth(client, "owner@example.com")
    resp = await o.get("/auth/ad/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ping"]["ok"] is True
    assert body["config"]["server"] == "dc01.school.test:636"
    assert body["config"]["bind_password_set"] is True
    assert "svc-password" not in str(body), "значение пароля сервисной учётки не должно попадать в ответ"
    assert body["problems"] == []

    t = await auth(new_client(client), "teacher1@example.com")
    assert (await t.get("/auth/ad/diagnostics")).status_code == 403


async def test_login_options_is_public_and_safe(client, seeded, fake_ad):
    anon = new_client(client)
    resp = await anon.get("/auth/login-options")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ldap_enabled": True,
        "local_enabled": True,
        "login_hint": "Доменная учётная запись: ivanov или ivanov@school.local",
        "password_managed_by": "Active Directory",
    }
    assert "dc01" not in str(body), "адрес контроллера домена не отдаётся анонимно"


# ------------------------------------------------------------- аудит и журнал
async def test_ad_login_writes_audit(client, seeded, fake_ad):
    await client.post("/auth/login", json={"email": "ivanov", "password": "DomainPass1!"})
    o = await auth(client, "owner@example.com")
    logs = (await o.get("/audit", params={"per_page": 100})).json()["items"]
    actions = [item["action"] for item in logs]
    assert "AD_PROVISION" in actions
    assert "LOGIN" in actions
    provision = next(i for i in logs if i["action"] == "AD_PROVISION")
    assert provision["changes"]["domain_account"] == "ivanov"
    assert provision["changes"]["role"] == "TEACHER"

    # неудачный вход тоже в журнале
    await client.post("/auth/login", json={"email": "ivanov", "password": "wrong"})
    logs2 = (await o.get("/audit", params={"action": "LOGIN_FAILED"})).json()["items"]
    assert logs2 and logs2[0]["changes"]["login"] == "ivanov"


# ------------------------------------------------------------- валидация логина
@pytest.mark.parametrize(
    "bad_login",
    ["", " ", "a", "user*)(injection", "user\nname", "x" * 300, "user name with spaces"],
)
async def test_login_field_is_validated(client, seeded, fake_ad, bad_login):
    resp = await client.post("/auth/login", json={"email": bad_login, "password": "whatever"})
    assert resp.status_code == 422, f"{bad_login!r} → {resp.status_code}"
    assert fake_ad.bind_calls == 0, "до валидации в каталог ходить нельзя"

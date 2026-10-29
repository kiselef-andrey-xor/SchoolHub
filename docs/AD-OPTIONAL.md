# Вход через Active Directory — необязательное расширение

> **По умолчанию выключено.** SchoolHub полностью работает на локальных учётных записях —
> этого достаточно для школы. Включайте доменную аутентификацию, только если действительно
> хотите, чтобы сотрудники входили под своими доменными паролями.
>
> Базовая установка: [`INSTALL.md`](INSTALL.md).

---

## 1. Что даёт интеграция

| Аспект | Поведение |
|---|---|
| Проверка пароля | LDAP/LDAPS **bind** под учёткой пользователя: пароль проверяет контроллер домена и **нигде не хранится** |
| Формат логина | `ivanov`, `ivanov@school.local`, `SCHOOL\ivanov` — любой из трёх |
| Кто может войти | Все пользователи домена (`LDAP_PROVISIONING=auto`) или только члены групп из `LDAP_ALLOWED_GROUPS` |
| Учётка в SchoolHub | Создаётся автоматически при первом входе |
| Роль при первом входе | `LDAP_DEFAULT_ROLE` (по умолчанию `TEACHER`); роли раздаёт Владелец в UI |
| Группы AD → роли | `LDAP_ADMIN_GROUPS` / `LDAP_OWNER_GROUPS` могут роль **только повышать** |
| Синхронизация | ФИО, должность (`title`), телефон, e-mail и список групп обновляются при каждом входе |
| Аварийный вход | Локальный Владелец (break-glass) работает при недоступном контроллере домена |
| Защита домена | Троттлинг неудачных попыток (`429`) **до** обращения к DC — иначе AD заблокирует учётку |
| Смена пароля | В UI отключена для доменных учётке (409/403) — пароль меняется в Windows (Ctrl+Alt+Del) |

---

## 2. Что понадобится

1. **LDAPS (порт 636)** или **LDAP + StartTLS (389)**. Простой LDAP без шифрования AD
   по умолчанию отклоняет (требует signing) — не используйте.
2. **Сервисная учётка только для чтения** — для поиска DN пользователя, чтения атрибутов
   и групп, а также для функции «Импорт из AD». Пароль пользователя ей не нужен.
3. **Корневой сертификат CA** в формате PEM, если сертификат DC выдан внутренним CA.
4. Доступ по TCP 636 (или 389) от NAS до контроллера домена.

---

## 3. Подготовка домена

### 3.1. Автоматически (PowerShell на контроллере домена)

```powershell
cd \\<путь к проекту>\deploy\synology
Set-ExecutionPolicy -Scope Process Bypass -Force
.\setup-ad.ps1 -DomainName school.local -BaseDN "DC=school,DC=local" -ServiceAccountName svc_schoolhub
```

Скрипт идемпотентен и:

1. создаёт OU `Service Accounts` и учётку `svc_schoolhub`
   (пароль не истекает, смена запрещена, **интерактивный вход запрещён**);
2. выдаёт ей `GenericRead` на базовый DN;
3. создаёт группы `SchoolStaff`, `SchoolAdmins`, `SchoolHub_Owners`;
4. проверяет доступность LDAPS и наличие сертификата на DC;
5. **печатает готовый блок для `.env`**.

### 3.2. Вручную (если скрипт запустить нельзя)

| Шаг | Действие |
|---|---|
| Учётка | ADUC → `Service Accounts` → New → User: `svc_schoolhub`; «Password never expires»; снять «User must change password»; в свойствах → Account → «Log On To» → запретить интерактивный вход |
| Права | ADUC → View → **Advanced Features** → свойства `DC=school,DC=local` → Security → Add → `svc_schoolhub` → только **Read** |
| Группы | `SchoolStaff` (все сотрудники школы), `SchoolAdmins` (завучи/администрация) |

> Сервисную учётку **нельзя** добавлять в Domain Admins — нужно только чтение.

### 3.3. Сертификат LDAPS

```powershell
# на DC: экспорт корневого сертификата CA
Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like "*school*" } |
  ForEach-Object { [IO.File]::WriteAllBytes("$env:TEMP\rootca.cer", $_.RawData) }
```

Конвертация DER → PEM (на любой машине с openssl, в том числе на NAS):

```bash
openssl x509 -inform der -in rootca.cer -out rootca.pem
```

Положите файл на NAS и подключите том в compose:

```yaml
    volumes:
      - /volume1/docker/schoolhub/certs:/certs:ro
```

Сертификат DC должен быть выдан на его **DNS-имя** — поэтому в `LDAP_SERVER` указывайте
имя (`dc01.school.local:636`), а не IP-адрес.

---

## 4. Включение в SchoolHub

Отредактируйте `.env` рядом с `deploy/synology/docker-compose.yml`:

```ini
LDAP_ENABLED=true
LDAP_SERVER=dc01.school.local:636
LDAP_USE_SSL=true
LDAP_DOMAIN=school.local
LDAP_BASE_DN=DC=school,DC=local
LDAP_BIND_DN=CN=svc_schoolhub,OU=Service Accounts,DC=school,DC=local
LDAP_BIND_PASSWORD=<пароль сервисной учётки>
LDAP_CA_CERT=/certs/rootca.pem
LDAP_ALLOWED_GROUPS=["SchoolStaff"]      # [] = пускать весь домен
LDAP_ADMIN_GROUPS=["SchoolAdmins"]       # повышение роли при входе
LDAP_PROVISIONING=auto
LDAP_DEFAULT_ROLE=TEACHER
```

В compose добавьте том с сертификатом (см. 3.3) и перезапустите:

```bash
cd /volume1/docker/schoolhub/app
sudo docker compose -f deploy/synology/docker-compose.yml up -d
sudo docker logs -f schoolhub-backend
```

**Как узнать свои параметры**, если не знаете:

```powershell
(Get-ADRootDSE).defaultNamingContext      # → DC=school,DC=local
(Get-ADDomainController).HostName         # → dc01.school.local
(Get-ADDomain).DNSRoot                    # → school.local
```

или на NAS: `nslookup -type=srv _ldap._tcp.school.local`.

---

## 5. Проверка

1. **В браузере** под Владельцем: раздел **Пользователи** → блок
   «Диагностика Active Directory» → «↻ Проверить». Покажет конфигурацию, список проблем
   настройки (`Не задан LDAP_BIND_DN`, `LDAP_TLS_VERIFY=none`…) и ответ контроллера домена.
2. **Через API**: `GET /api/v1/auth/ad/diagnostics` (тот же JSON; значение пароля
   сервисной учётки не отдаётся, только признак `bind_password_set`).
3. **Входом**: на экране входа появится подсказка «Доменная учётная запись: ivanov или
   ivanov@school.local». Залогиньтесь под рядовым сотрудником — он должен попасть в систему
   с ролью «Учитель», а его ФИО и должность подтянутся из AD.
4. **Сетевая доступность** из контейнера:

```bash
sudo docker exec -it schoolhub-backend getent hosts dc01.school.local
sudo docker exec -it schoolhub-backend python -c "import socket;socket.create_connection(('dc01.school.local',636),5);print('OK')"
```

---

## 6. Импорт сотрудников до их первого входа

Нужен, чтобы **заранее назначить роль** (иначе завуч при первом входе получит «Учитель»):

**Пользователи → 🌐 Импорт из AD** → поиск по ФИО/логину → отметить → выбрать роль → Импорт.

Операция идемпотентна: уже существующие попадают в `skipped`, дубли внутри запроса
игнорируются, ошибки по каждому пользователю возвращаются отдельно.

Те же действия через API:

```bash
curl -H "Authorization: Bearer $TOKEN" "$API/users/ad/search?q=Морозова"
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"usernames":["zavuch","ivanov"],"role":"ADMIN"}' "$API/users/ad/import"
```

---

## 7. Как устроен вход (для администратора)

```
логин (ivanov | ivanov@school.local | SCHOOL\ivanov)
  → валидация набора символов + экранирование для LDAP-фильтра
  → троттлинг: не более LOGIN_RATE_LIMIT неудачных попыток за LOGIN_RATE_WINDOW_MIN минут
  → найден ли пользователь в SchoolHub с auth_source=local?
       да  → проверяем ЛОКАЛЬНЫЙ пароль, в каталог НЕ обращаемся
       нет → сервисная учётка: поиск DN + чтение атрибутов (displayName, mail, title, memberOf)
  → bind под учёткой пользователя с введённым паролем   ← фактическая проверка доменом
  → проверка userAccountControl (не отключён) и белого списка групп AD
  → провижининг/синхронизация учётки SchoolHub (роль из групп — только повышение)
  → выдача JWT
```

Почему локальная проверка идёт первой: иначе локальную учётку можно было бы использовать
как канал подбора доменных паролей.

Ошибки AD переводятся в понятные сообщения:

| Код | Сообщение |
|---|---|
| `52e` | Неверное имя пользователя или пароль |
| `532` | Срок действия пароля истёк |
| `533` | Учётная запись заблокирована администратором домена |
| `701` | Срок действия учётной записи истёк |
| `773` | Требуется смена пароля |
| `775` | Учётная запись заблокирована из-за превышения числа попыток входа |

---

## 8. Все параметры LDAP

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LDAP_ENABLED` | `false` | Включить интеграцию |
| `LDAP_SERVER` | — | `dc01.school.local:636` (DNS-имя, не IP) |
| `LDAP_USE_SSL` | `true` | `true` = LDAPS(636), `false` = LDAP(389)+StartTLS |
| `LDAP_USE_STARTTLS` | `true` | Применяется при `LDAP_USE_SSL=false` |
| `LDAP_TLS_VERIFY` | `required` | `none` — **только для отладки** |
| `LDAP_CA_CERT` | — | Путь к PEM внутри контейнера (`/certs/rootca.pem`) |
| `LDAP_DOMAIN` | — | `school.local` — для суффикса e-mail и формата входа |
| `LDAP_BASE_DN` | — | `DC=school,DC=local` |
| `LDAP_BIND_DN` | — | Сервисная учётка (read-only) |
| `LDAP_BIND_PASSWORD` | — | Её пароль |
| `LDAP_LOGIN_FORMAT` | `upn` | `upn` / `downlevel` / `both` |
| `LDAP_PROVISIONING` | `auto` | `auto` — создавать при первом входе; `manual` — только импортированные |
| `LDAP_DEFAULT_ROLE` | `TEACHER` | Роль нового пользователя |
| `LDAP_ALLOWED_GROUPS` | `[]` | Белый список групп (пусто = весь домен) |
| `LDAP_ADMIN_GROUPS` | `[]` | Группы, дающие роль ADMIN |
| `LDAP_OWNER_GROUPS` | `[]` | Группы, дающие роль OWNER (используйте осторожно) |
| `LDAP_ROLE_FROM_GROUPS` | `upgrade` | `upgrade` — группы только повышают роль; `off` — роли только вручную |
| `LDAP_SYNC_GROUPS_ON_LOGIN` | `true` | Обновлять список групп при входе |
| `LDAP_SYNC_ATTRS_ON_LOGIN` | `true` | Обновлять ФИО/должность/телефон/e-mail при входе |
| `LDAP_TIMEOUT_SEC` | `10` | Таймаут подключения к DC |
| `LDAP_RESOLVE_GROUP_SIDS` | `false` | Резолвить SID групп (нужно для вложенных групп) |
| `LOGIN_RATE_LIMIT` | `10` | Неудачных попыток до блокировки |
| `LOGIN_RATE_WINDOW_MIN` | `15` | Окно подсчёта попыток, минут |

Маппинг атрибутов AD (`LDAP_ATTR_MAP` в `app/config.py`) по умолчанию:
`sAMAccountName → логин`, `userPrincipalName → UPN`, `mail → e-mail`,
`displayName → ФИО`, `title → должность`, `telephoneNumber → телефон`, `memberOf → группы`.

---

## 9. Диагностика и типовые проблемы

| Симптом | Причина | Решение |
|---|---|---|
| `503 AD_ERROR`, «Контроллер домена недоступен» | DNS/файрвол | `getent hosts dc01.school.local` в контейнере; откройте 636/tcp от NAS до DC |
| «Ошибка TLS … certificate verify failed» | самоподписанный CA / сертификат на IP | положите `rootca.pem` в `certs/`, в `LDAP_SERVER` укажите DNS-имя; для отладки временно `LDAP_TLS_VERIFY=none` |
| «DC требует LDAP signing» | попытка простого LDAP на 389 | включите LDAPS или StartTLS |
| «Сервисная учётка LDAP отклонена» | неверный `LDAP_BIND_DN`/пароль | проверьте DN целиком (`CN=…,OU=…,DC=…`), учётка не должна быть заблокирована |
| Вход есть, но роль «Учитель» у завуча | не настроен маппинг | `LDAP_ADMIN_GROUPS=["SchoolAdmins"]` или импорт с ролью через UI |
| «Доступ разрешён только сотрудникам школы» | сработал `LDAP_ALLOWED_GROUPS` | добавьте пользователя в группу из белого списка |
| `429 RATE_LIMITED` | перебор пароля | подождите `LOGIN_RATE_WINDOW_MIN` минут. **Не отключайте** троттлинг — иначе AD заблокирует учётку |
| `409` при смене пароля в профиле | учётка доменная | пароль меняется в AD (Ctrl+Alt+Del) |
| Не видно реального IP в журнале ознакомления | нет `X-Forwarded-For` | `TRUST_PROXY_HEADERS=true` + проброс заголовка в reverse proxy |

Журнал событий содержит `LOGIN` (с указанием источника: `local`/`ldap`), `LOGIN_FAILED`,
`AD_PROVISION`, `AD_ROLE_UPGRADE`, `AD_IMPORT` — с IP и составом групп.

---

## 10. Откат к локальным учёткам

```ini
LDAP_ENABLED=false
```

и `sudo docker compose … up -d`. Уже созданные учётки останутся в базе, но вход по ним
прекратится (их пароль хранится как `!ldap`). Чтобы вернуть сотруднику доступ:

```sql
-- psql внутри контейнера БД
UPDATE users SET auth_source='local', password_hash='!reset' WHERE email='ivanov@school.local';
```

после чего администратор задаёт новый пароль в разделе «Пользователи».
Локальный Владелец (break-glass) работает независимо от `LDAP_ENABLED`.

---

## 11. Безопасность

* Пароли домена **не хранятся и не логируются**; в БД маркер `!ldap`.
* Сервисная учётка — только `GenericRead`, интерактивный вход запрещён, не в Domain Admins.
* LDAP-фильтры строятся только из экранированных значений (`escape_filter_chars`),
  логин дополнительно валидируется по набору символов.
* `LDAP_TLS_VERIFY=required` в боевой среде; `none` — только для отладки.
* Пароль сервисной учётки не возвращается API (только `bind_password_set`).
* Троттлинг входа защищает доменные учётки от блокировки политикой AD.
* Неудачные попытки входа пишутся в журнал отдельной транзакцией — след остаётся даже при отказе.

---

## 12. Тесты

Сценарии доменной аутентификации покрыты тестами на заглушке каталога
(`tests/test_ad_auth.py`, 16 тестов) — реальный контроллер домена для прогона не нужен:

```bash
cd backend && pytest tests/test_ad_auth.py -q
```

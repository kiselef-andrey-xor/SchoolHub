# Как самостоятельно вносить небольшие изменения в SchoolHub

Памятка для администратора школы, который не боится текстовых файлов.
Всё ниже — про **небольшие** правки: надписи, подсказки, порядок колонок, значения
по умолчанию. Для новых сущностей и таблиц лучше звать разработчика: там легко
забыть миграцию и сломать боевую базу.

---

## 1. Где что лежит

```
schoolhub/
├── backend/
│   └── app/
│       ├── api/          # эндпоинты: ЧТО отвечает сервер на запросы (/users, /orders, …)
│       ├── services/     # бизнес-логика: проверки прав, уведомления, парсинг, напоминания
│       ├── models/       # таблицы базы данных (колонки и связи)
│       ├── schemas/      # формы запросов/ответов (валидация полей)
│       ├── migrations.py # самописные идемпотентные миграции (шаги m001…m027)
│       └── alembic/      # ревизии Alembic (0001…0020) — ВТОРАЯ ветка миграций
├── frontend/
│   └── src/
│       ├── pages/        # экраны: UsersPage, TasksPage, MovementPage, …
│       ├── components/   # переиспользуемые блоки: Modal, UserMultiSelect, …
│       ├── api/          # endpoints.ts — список запросов к серверу; client.ts — HTTP-слой
│       ├── lib/          # format.ts (подписи ролей/статусов), orderTypes.ts, permissions.ts
│       └── types.ts      # TypeScript-типы (формы данных)
└── deploy/synology/      # compose для NAS, backup-db.sh, restore-db.sh
```

Правило проекта: **надписи и подписи** живут в `frontend/src/lib/format.ts` и в самих
страницах; **права** — в `backend/app/capabilities.py` (+ зеркало `frontend/src/lib/permissions.ts`).

---

## 2. Запустить проект у себя для правок (без NAS)

```bash
# бэкенд (Python 3.11+): база поднимется на SQLite автоматически
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# фронтенд (Node 20+), в отдельном терминале
cd frontend
npm install
npm run dev            # откроется http://localhost:5173, API проксируется на :8000
```

Первый владелец создаётся через мастер первоначальной настройки в браузере
(код — в журнале контейнера/консоли uvicorn) или скриптом:
`python -m scripts.first_owner --email admin@school.local --password 'Пароль123!' --name 'Админ'`.

Проверки перед выкаткой на NAS:

```bash
cd backend  && python -m pytest tests -q        # все тесты должны быть зелёными
cd frontend && npm run typecheck && npm run build
```

---

## 3. Выкатка изменений на NAS

1. Скопируйте изменённые файлы в `/volume2/docker/schoolhub/app` (ваш путь тома).
2. `sudo docker compose -f deploy/synology/docker-compose.yml up -d --build`
3. `sudo docker logs -f schoolhub-backend` — дождаться «DB schema ready» и отсутствия ошибок.
4. Перед любыми правками схемы БД — резервная копия: профиль Владельца →
   «Скачать резервную копию БД» или `backup-db.sh`.

---

## 4. Рецепты частых правок

### 4.1. Поменять надпись/подсказку на экране
Найдите текст поиском по папке `frontend/src` (VS Code: Ctrl+Shift+F или `grep -R "текст" frontend/src`)
и отредактируйте строку. Подписи ролей/статусов/приоритетов — в `frontend/src/lib/format.ts`.

### 4.2. Поменять название школы в печатных формах
Не правка кода: Профиль → «Данные школы» (Владелец).

### 4.3. Добавить/удалить тип приказа
Не правка кода: Приказы → «⚙ Типы приказов» (ADMIN/OWNER). Удалить занятый тип нельзя —
сначала переведите приказы на другой тип.

### 4.4. Изменить значение по умолчанию (пример: срок токена, лимиты)
`backend/app/config.py` — все настройки читаются из ENV с дефолтами в коде.
На NAS переопределяются строками в `.env` рядом с compose-файлом
(например `ACCESS_TOKEN_TTL_MIN=60`), затем `up -d` (пересборка не нужна).

### 4.5. Добавить КОЛОНКУ в таблицу списка
1. `frontend/src/pages/…Page.tsx`: в `<thead>` добавить `<th>`, в `<tbody>` — `<td>`;
2. если данных нет в ответе API — добавить поле по цепочке из рецепта 4.6.

### 4.6. Добавить ПОЛЕ сущности (полная цепочка, не пропустить звенья!)
1. `backend/app/models/<сущность>.py` — колонка (`nullable=True` или `default`, чтобы не сломать старые строки);
2. **МИГРАЦИИ — ОБЕ ВЕТКИ ОБЯЗАТЕЛЬНО**:
   * `backend/app/migrations.py`: новый шаг `_m0XX_…` + регистрация в словаре `MIGRATIONS`
     (шаг сам проверяет `_has_column` и ничего не ломает при повторе);
   * `backend/alembic/versions/00XX_….py`: ревизия с тем же изменением
     (`down_revision` = предыдущая ревизия, проверки `_has_column/_has_table`);
     НОВЫЕ ТАБЛИЦЫ создавайте ТОЛЬКО из metadata моделей
     (`Model.__table__.create(bind=op.get_bind(), checkfirst=True)`) — НЕ пишите
     `sa.CHAR(36)`/`sa.Uuid` вручную: GUID в PostgreSQL есть UUID, а в SQLite CHAR(36),
     и внешний ключ CHAR(36) → users.id (UUID) на PostgreSQL не создаётся
     («foreign key constraint cannot be implemented») — backend падает при старте
     (инцидент 13.10.2026 с ревизией 0010, исправлено редакцией 2; тесты на SQLite
     такое НЕ ловят — проверяйте новые ревизии на PostgreSQL);
   Боевые базы на NAS идут через Alembic, тестовые/старые — через самописные шаги:
   пропустите ветку — получите «column does not exist» на боевом сервере (уже было однажды).
   Проверка: `cd backend && python -m pytest tests/test_schema_drift.py -q` — сверяет
   схему, собранную ОБОИМИ путями, с моделями и лечит «старую» базу; в CI это отдельный шаг.
   Подробно о том, кто и когда что применяет, — `docs/ARCHITECTURE.md`, ADR-14.
3. `backend/app/schemas/<…>.py` — поле в In/Out-схемах;
4. `backend/app/api/<…>.py` — прочитать/записать поле в хендлере;
5. `frontend/src/types.ts` — поле в интерфейсе;
6. `frontend/src/api/endpoints.ts` — передать в payload;
7. страница/форма —.input и отображение.
8. Тест в `backend/tests/` на новое поведение.

### 4.7. Добавить кнопку-действие
Кнопка в странице → функция → вызов `xxxApi.метод(...)` → после успеха
`invalidate()/refetch()` для обновления списка. Сам метод добавить в
`frontend/src/api/endpoints.ts`, эндпоинт — в `backend/app/api/…py`
(не забудьте проверку прав `require_role` / `actor.role` и запись в аудит).

### 4.8. Изменить права доступа
`backend/app/capabilities.py` (матрица ROLE_CAPABILITIES) + при необходимости
персональные гранты: колонка-флаг в `users` (см. `incoming_journal_access`,
`movement_access`, `equipment_access`, `notes_full_access`, `substitutions_access`,
`helpdesk_access`) + чекбокс в `UsersPage` (список в блоке «Статус и доступы») +
дописка capability в `GET /auth/me` (`backend/app/api/auth.py`).

Пример из v1.12 — грант «системный администратор» для техподдержки:
`users.helpdesk_access` (модель) → шаг `m026` + ревизия `0019` (миграции, обе ветки)
→ `HELPDESK_MANAGE` в `capabilities.py` + зеркало `helpdeskManage` в
`frontend/src/lib/permissions.ts` → `helpdesk_access` в схемах
`UserUpdateIn`/`UserOut` → чекбокс в `UsersPage` → тест в `backend/tests/test_helpdesk.py`.

---

## 5. Чего делать НЕ стоит

* Править базу руками (psql/SQL-консоль DSM) — только через миграции.
* Удалять/переименовывать колонки без резервной копии.
* Править `frontend/dist` и содержимое контейнеров: всё сгорит при пересборке.
* Отключать троттлинг входа и `LDAP_*`, если не понимаете последствий.
* Коммитить пароли/ключи в файлы репозитория (секреты — только в `.env` на NAS).

---

## 6. Если после правки что-то сломалось

1. `sudo docker logs schoolhub-backend --tail 100` — текст ошибки почти всегда понятен
   (NameError/IntegrityError/«column does not exist» → смотрите раздел 4.6, пункт 2).
2. Откат: вернуть прежние файлы и `up -d --build`; база откатывается из дампа
   (`restore-db.sh`), если менялась схема.
3. Тесты локально экономят час отладки на NAS: `python -m pytest tests -q`.

## 7. Коммиты и проверки перед пушем

Один раз выполните `make precommit` — git-хуки будут прогонять перед каждым коммитом
`ruff`, проверку дрейфа схемы (если тронуты модели/миграции), поиск захардкоженных
секретов и `tsc --noEmit` (если тронут фронтенд); перед пушем — `pytest`. Это те же
проверки, что в CI, поэтому «локально прошло — в CI упало» больше не повторится.

Соглашение о сообщениях — [Conventional Commits](https://www.conventionalcommits.org/):

```
<тип>(<область>): <что сделано, в повелительном наклонении>

feat(help): видимость статей справки — всем / администрации / Владельцу
fix(lockdown): режим «Локдаун» применяется к CSV-выгрузкам и печатным формам
docs(readme): шаг CI описан как фактический
test(migrations): проверка дрейфа схемы против моделей
```

Типы: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `chore`, `security`.
Область — модуль (`help`, `orders`, `auth`, `nginx`, `ci`). Subject — до 72 символов,
подробности — в теле коммита через пустую строку.

Чего делать не стоит:

* загружать код через веб-интерфейс GitHub («Add files via upload»): такие коммиты
  ломают `git blame` и `git bisect` — история проекта живёт годами;
* складывать несколько несвязанных правок в один коммит;
* коммитить `frontend/dist`, `tsconfig.tsbuildinfo`, `.env` с реальными значениями
  (все три закрыты корневым `.gitignore`; проверка — `bash scripts/check-no-build-artifacts.sh`).

Перед пушем полезно прогнать всё сразу: `make check` (чистое окружение → импорт →
`ruff` → `pytest` → схема → типы → сборка → гигиена репозитория).

---

Удачи! Маленькие правки — это нормально: проект собран из небольших
понятных модулей, чтобы их можно было трогать по одному.

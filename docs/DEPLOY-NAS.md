# Развёртывание SchoolHub на Synology NAS — справочник

> 🚀 **Быстрая установка (5 шагов): [`INSTALL.md`](INSTALL.md)** — начните с неё.
> Здесь — справочная информация: устройство стенда, тома, HTTPS, бэкапы, обновление,
> диагностика, установка без Docker и чек-лист безопасности.
>
> Вход через Active Directory — отдельный необязательный модуль: [`AD-OPTIONAL.md`](AD-OPTIONAL.md).

Целевое железо: **Synology DS223** — Realtek RTD1619B (ARMv8/arm64), 2 ГБ RAM, DSM 7.2+,
Container Manager. Все образы мультиархитектурные, собираются на NAS напрямую.

---

## 1. Схема стенда

```
        сотрудники (браузер)
                │  https://schoolhub.school.local     ← сертификат Let's Encrypt от DSM
                ▼
   ┌──────────────────────────────┐
   │ DSM Reverse Proxy (TLS)      │   Панель управления → Вход в систему → Обратный прокси
   └──────────────┬───────────────┘
                  │ http://localhost:8080
   ┌──────────────▼───────────────┐     ┌────────────────────────┐
   │ nginx-контейнер (frontend)   │────►│ backend (FastAPI)      │
   │  статика SPA + /api → backend│     │  :8000, наружу НЕ виден│
   └──────────────────────────────┘     └───────┬────────────────┘
                                                │ asyncpg
                                    ┌───────────▼──────────┐
                                    │ PostgreSQL 16        │  :5432 наружу НЕ виден
                                    └──────────────────────┘

   Тома Docker:  dbdata (БД) · uploads (PDF/DOCX приказов) · secrets (SECRET_KEY)
```

Наруж публикуется **только один порт** — `HTTP_PORT` (8080) контейнера nginx.
Backend и PostgreSQL доступны лишь внутри сети контейнеров `schoolhub`, поэтому
CORS не требуется и API не торчит в сеть.

---

## 2. Файлы развёртывания

```
deploy/synology/
├── docker-compose.yml       # 3 сервиса, лимиты памяти, именованные тома
├── .env.nas.example         # необязательные настройки (копируется в .env при установке)
├── dsm-reverse-proxy.conf   # вставка в «Пользовательскую конфигурацию» прокси DSM
├── backup-db.sh             # pg_dump + sha256 + ротация (для планировщика DSM)
├── check-health.sh          # диагностика: контейнеры, RAM, БД, LDAP, права, место
└── setup-ad.ps1             # подготовка домена (нужна только при включённом AD)
install-nas.sh               # установка одной командой (в корне проекта)
```

---

## 3. Тома и где что лежит

| Том | Монтируется в | Содержимое |
|---|---|---|
| `dbdata` | `/var/lib/postgresql/data` | База PostgreSQL |
| `uploads` | `/data/uploads` | Документы приказов (PDF/DOCX), `<yyyy>/<mm>/<uuid>.<ext>` |
| `secrets` | `/data/secrets` | `secret_key` — автоматически сгенерированный ключ подписи JWT |

Тома именованные, физически лежат в `/volume1/@docker/volumes/schoolhub_*`.
Увидеть их можно так:

```bash
sudo docker volume ls | grep schoolhub
sudo docker volume inspect schoolhub_uploads --format '{{ .Mountpoint }}'
```

> **Важно:** потеря тома `secrets` = инвалидация всех сессий (пользователи перезалогинятся).
> Потеря `uploads` = потеря документов приказов при сохранившихся записях в БД.
> Поэтому оба тома должны быть в бэкапе (см. §6).

---

## 4. Память и производительность на 2 ГБ

| Контейнер | Лимит в compose | Типичное потребление |
|---|---|---|
| `schoolhub-db` | 420 МБ | 90–180 МБ |
| `schoolhub-backend` | 460 МБ | 130–220 МБ |
| `schoolhub-frontend` | 96 МБ | 8–15 МБ |
| **Итого** | **~980 МБ** | **~250–400 МБ** |

Принятые решения:

* **один воркер uvicorn** — на 2 ГБ больше не нужно, школа (50–300 сотрудников,
  десятки одновременных сессий) обслуживается без нагрузки;
* `shared_buffers=128MB`, `max_connections=40` — PostgreSQL не съедает память;
* **LibreOffice — облегчённый nogui-вариант** (`libreoffice-writer-nogui`, ~300 МБ образа):
  Word-документы приказов и задач конвертируются в PDF ФОНОВО сразу при загрузке и кэшируются,
  поэтому просмотр в браузере открывается мгновенно, а нагрузка на 2 ГБ RAM возникает
  только в момент загрузки документа (см. `app/services/documents.py`);
* статика nginx кэшируется на 7 дней (имена файлов содержат хеш сборки);
* тяжёлые операции (выгрузка CSV, конвертация) не блокируют event loop.

Если памяти всё же не хватает: `shared_buffers=64MB`, `MAX_UPLOAD_MB=15`,
и выключите DSM-пакеты, которые не используете.

---

## 5. HTTPS через DSM Reverse Proxy

1. **DSM → Панель управления → Безопасность → Сертификат** → Добавить → Let's Encrypt →
   `schoolhub.ваш-домен` (нужна доступность порта 80 из интернета либо DNS-01 challenge).
2. **DSM → Панель управления → Вход в систему → Дополнительно → Обратный прокси** → Создать:

   | Поле | Значение |
   |---|---|
   | Описание | `SchoolHub` |
   | Источник | `HTTPS`, `schoolhub.ваш-домен`, порт `443` |
   | Назначение | `HTTP`, `localhost`, порт `8080` |

3. В правиле → **Пользовательская конфигурация** → вставьте
   [`deploy/synology/dsm-reverse-proxy.conf`](../deploy/synology/dsm-reverse-proxy.conf).
   Там: проброс `X-Forwarded-For`/`X-Forwarded-Proto`, лимит загрузки 30 МБ,
   таймауты 300 с, HSTS.

   > `X-Forwarded-For` **обязателен**: по нему в листе ознакомления фиксируется реальный
   > IP сотрудника (юридически значимый след). В backend уже включён `TRUST_PROXY_HEADERS=true`.
   > Без него в журнале будет IP контейнера nginx.

4. **DNS**: A-запись `schoolhub` → IP NAS (внутренняя зона домена или публичная).
5. **Фаервол DSM**: разрешите 443 (и 22 только для доверенной подсети администрирования).
6. **`.env` проекта** (рядом с `docker-compose.yml`) — чтобы сессии и кросс-доменные
   запросы корректно работали по HTTPS:

   ```dotenv
   TRUST_PROXY_HEADERS=true
   COOKIE_SECURE=true
   CORS_ORIGINS=["https://schoolhub.ваш-домен"]
   ```

   * `TRUST_PROXY_HEADERS=true` — доверять заголовкам `X-Forwarded-For`/`X-Forwarded-Proto`
     от прокси (в compose уже включено по умолчанию; здесь — для полноты картины);
   * `COOKIE_SECURE=true` — сессионные cookie только по HTTPS. Значение `auto`
     (по умолчанию) тоже работает: флаг ставится по протоколу запроса, который
     DSM пробрасывает в `X-Forwarded-Proto`;
   * `CORS_ORIGINS` — адрес(-а), по которым систему открывают в браузере, вместе
     со схемой и портом (например `["https://atom-school.local:9090"]`). С v1.10.0
     compose пробрасывает переменную в контейнер backend (раньше она в `.env`
     игнорировалась); принимается и перечисление через запятую без скобок.
     Штатно фронтенд и API на одном origin (nginx фронтенда проксирует `/api`),
     поэтому переменность нужна в основном при нестандартных схемах доступа.

   После правки `.env` пересоздайте контейнеры: `docker compose up -d`
   (пересборка образов не требуется — меняются только переменные окружения).

---

## 6. Бэкап и восстановление

### 6.1. База данных

Разовая копия перед изменением: в приложении «Профиль → Резервное копирование →
⬇ Скачать резервную копию БД» (только Владелец; нужен postgresql-client в образе —
включён в Dockerfile). Восстановление дампа:

```bash
sudo bash deploy/synology/restore-db.sh /volume1/backup/schoolhub/schoolhub-XXXX.sql
```

```bash
sudo cp /volume1/docker/schoolhub/app/deploy/synology/backup-db.sh /volume1/docker/schoolhub/
sudo chmod +x /volume1/docker/schoolhub/backup-db.sh
sudo bash /volume1/docker/schoolhub/backup-db.sh          # тестовый прогон
```

**DSM → Панель управления → Планировщик задач → Создать → Запланированная задача →
Скрипт, определённый пользователем**: пользователь `root`, путь
`/volume1/docker/schoolhub/backup-db.sh`, ежедневно 03:30.

Скрипт делает `pg_dump | gzip`, пишет sha256, проверяет целостность архива и хранит
14 дневных + 8 недельных копий в `/volume1/backup/schoolhub`.

### 6.2. Файлы и секреты

Включите в **Hyper Backup** том `/volume1/@docker/volumes` (там `uploads` и `secrets`)
и папку `/volume1/backup/schoolhub` (дампы). Либо настройте **Snapshot Replication**
на Btrfs-томе — но не снимайте «горячие» снапшоты `pgdata`: используйте дампы.

### 6.3. Восстановление

```bash
cd /volume1/docker/schoolhub/app
sudo docker compose -f deploy/synology/docker-compose.yml up -d db backend
gunzip -c /volume1/backup/schoolhub/schoolhub-YYYYMMDD-HHMMSS.sql.gz | \
  sudo docker exec -i schoolhub-db psql -U schoolhub -d schoolhub
# вернуть uploads/ и secrets/ из Hyper Backup
sudo docker compose -f deploy/synology/docker-compose.yml restart backend
```

Проверка: `GET /api/v1/health`, вход под Владельцем, открытие PDF произвольного приказа.

---

## 7. Обновление версии

```bash
cd /volume1/docker/schoolhub/app
git pull                                                  # или заменить файлы из архива
sudo bash /volume1/docker/schoolhub/backup-db.sh          # бэкап перед обновлением
sudo docker compose -f deploy/synology/docker-compose.yml up -d --build
sudo bash deploy/synology/check-health.sh
```

Схема БД приводится в актуальное состояние **автоматически** при старте backend —
схемой управляет **Alembic** (`backend/alembic/`, базовая ревизия `0001_baseline`):

* новая установка → `alembic upgrade head` создаёт все таблицы;
* обновление со старой версии (схема была создана `create_all`) → приложение само
  добирает недостающие таблицы, переносит данные legacy-шагами и штампует базу на `head`;
* повторный запуск ничего не меняет (идемпотентно).

Ручное управление (не обязательно на NAS):

```bash
cd backend
alembic current                                  # текущая ревизия
alembic upgrade head                             # применить миграции
alembic revision --autogenerate -m "описание"    # новая миграция при изменении моделей
```

Для крупных инсталляций автоприменение можно выключить (`AUTO_MIGRATE=false`) и накатывать
миграции из CI/CD. Откат версии приложения: `git checkout <тег>` → `up -d --build`;
если новая версия добавляла колонки, старая продолжит работать (лишние колонки игнорируются).

Актуальные ревизии: `0001_baseline` (все таблицы), `0002_local_accounts`
(`users.login`, `users.avatar`, `email` допускает NULL — локальные учётки без e-mail).
Проверить, что образ собран из свежих файлов:

```bash
sudo docker exec schoolhub-backend ls /code/alembic/versions/   # ожидаем обе ревизии
sudo docker logs schoolhub-backend 2>&1 | grep -i alembic       # виден применённый upgrade
```

### 7.1. Полный сброс: чистая база с нуля

> Теряются ВСЕ данные (пользователи, приказы, задачи, журнал). Применяйте, когда
> данных нет или они не нужны. Бэкап перед сбросом: `sudo bash deploy/synology/backup-db.sh`.

Способ А — удалить тома Docker (рекомендуется):

```bash
cd /volume2/docker/schoolhub/app                    # или /volume1 — ваш том
sudo docker compose -f deploy/synology/docker-compose.yml down
sudo docker volume ls | grep schoolhub              # имена томов: schoolhub_db_v2, schoolhub_uploads_v2, schoolhub_secrets_v2
sudo docker volume rm schoolhub_db_v2 schoolhub_uploads_v2 schoolhub_secrets_v2
sudo docker rmi schoolhub-backend:local schoolhub-frontend:local   # удалить ОБРАЗЫ, чтобы пересборка была честной
sudo docker compose -f deploy/synology/docker-compose.yml up -d --build
```

Способ Б — «новая база» правкой compose (старые тома остаются нетронутыми, их можно
удалить позже): в `docker-compose.yml` замените имя тома БД в двух местах —
`db_v2:/var/lib/postgresql/data` → `db_v3:/var/lib/postgresql/data` и в блоке
`volumes:` внизу `db_v2:` → `db_v3:`. Затем `up -d --build`: PostgreSQL увидит пустой
каталог и инициализирует новый кластер.

После сброса стек стартует с пустой БД и возвращается в режим первоначальной настройки:
backend напечатает в журнал одноразовый код (Container Manager → Контейнер →
schoolhub-backend → Журнал), вы открываете сайт и создаёте Владельца сами.
Чтобы пропустить этот шаг, задайте `OWNER_EMAIL` / `OWNER_PASSWORD` в `.env` рядом с
compose-файлом — их читает `app/services/bootstrap.py::ensure_owner_from_env`.

> Учётные данные Владельца и `SECRET_KEY` **не должны** лежать в compose-файле:
> репозиторий публичный. Ключ подписи генерируется при первом старте и сохраняется
> в томе `secrets_v2` (`/data/secrets/secret_key`, права 0600), поэтому переживает
> пересоздание контейнера. См. `review/REVIEW.md` §P0-3.

Те же действия в DSM UI: Container Manager → Контейнер (остановить/удалить schoolhub-*)
→ Хранилище/Тома (удалить schoolhub_db_v2 и соседние) → Образ (удалить
schoolhub-backend:local) → Проект → запустить.

После сброса SECRET_KEY тоже следует удалить, если хотите начать «с чистого листа»:
`sudo docker volume rm schoolhub_secrets_v2` — все выданные ранее токены перестанут
проверяться.

---

## 8. Эксплуатационные команды

| Операция | Команда |
|---|---|
| Статус контейнеров | `sudo docker compose -f deploy/synology/docker-compose.yml ps` |
| Логи backend | `sudo docker logs -f schoolhub-backend` |
| Логи nginx | `sudo docker logs -f schoolhub-frontend` |
| Перезапуск backend | `sudo docker restart schoolhub-backend` |
| Остановить всё | `sudo docker compose -f deploy/synology/docker-compose.yml down` |
| Остановить и удалить данные | `… down -v` ⚠️ удалит тома |
| psql | `sudo docker exec -it schoolhub-db psql -U schoolhub -d schoolhub` |
| Диагностика | `sudo bash deploy/synology/check-health.sh` |
| Бэкап БД | `sudo bash /volume1/docker/schoolhub/backup-db.sh` |
| Код первоначальной настройки | `sudo docker exec -it schoolhub-backend python -m scripts.first_owner --show-token` |
| Сброс пароля Владельца | `sudo docker exec -it schoolhub-backend python -m scripts.first_owner --force --email … --password …` |
| Тесты после изменений | `sudo docker compose exec backend pytest -q` |
| Потребление ресурсов | `sudo docker stats` |

---

## 9. Диагностика

| Симптом | Причина | Решение |
|---|---|---|
| Страница не открывается | контейнеры не поднялись / порт занят | `docker ps`; `netstat -tulpn \| grep 8080`; поменяйте `HTTP_PORT` в `.env` |
| Белый экран, в консоли 404 на `/assets/*` | фронтенд собран со старым base | пересоберите: `up -d --build frontend` |
| API отдаёт 502 | backend не стартовал | `docker logs schoolhub-backend` (обычно ошибка подключения к БД) |
| «Система ещё не настроена» | не создан Владелец | код в журнале или `first_owner --show-token` |
| `503 SETUP_REQUIRED` на всех страницах | настройка не завершена | создайте Владельца через веб-форму или CLI |
| Не загружается PDF > 25 МБ | лимит приложения/nginx | `MAX_UPLOAD_MB` в `.env` + `MAX_UPLOAD_SIZE` у frontend |
| В журнале ознакомления IP = 172.x.x.x | нет `X-Forwarded-For` | включите `dsm-reverse-proxy.conf` (§5) |
| Даты в журнале сдвинуты на часы | не задан `TZ` | укажите `TZ=Europe/Moscow` в `.env` |
| Медленно открывается список приказов | большой объём документов | проверьте `docker stats`; PDF отдаётся потоково, кэш 60 с |
| `docker compose` не найден | старый Container Manager | обновите пакет в Центре пакетов |

Полный прогон диагностики: `sudo bash deploy/synology/check-health.sh`
(контейнеры, RAM, место, health API, состояние БД и счётчики, LDAP если включён,
права на каталоги, последние события).

---

## 10. Установка без Docker (если Container Manager недоступен)

DSM не поддерживает произвольные systemd-сервисы, поэтому запускаем через
**Планировщик задач** (задача «при загрузке» + автоперезапуск).

```bash
# 1) Python 3.11 из Центра пакетов (или python3 из DSM) и Node 20 для сборки фронта
sudo -i
mkdir -p /volume1/apps/schoolhub && cd /volume1/apps/schoolhub
# скопируйте backend/ и frontend/ сюда

# 2) зависимости
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt

# 3) БД: поднимите PostgreSQL в контейнере или используйте внешний сервер
export DATABASE_URL="postgresql+asyncpg://schoolhub:ПАРОЛЬ@db-host:5432/schoolhub"
export UPLOAD_DIR=/volume1/apps/schoolhub/uploads
export APP_ENV=prod

# 4) сборка статики фронтенда
cd frontend && npm ci && npm run build && cd ..

# 5) схема + первый Владелец
cd backend && ../.venv/bin/python -c "import asyncio;from app.database import init_db;asyncio.run(init_db())"
../.venv/bin/python -m scripts.first_owner --email admin@school.local --password 'Пароль12345' --name 'Администратор'

# 6) запуск
nohup ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 > uvicorn.log 2>&1 &
```

Статику раздаёт веб-станция DSM (или nginx-пакет) с проксированием `/api` на `127.0.0.1:8000`.
Для автозапуска создайте задачу в **Планировщике DSM** (триггер «Загрузка»), которая
выполняет скрипт запуска из шага 6.

> Docker-путь предпочтительнее: изоляция, healthcheck'и, простая миграция между NAS.

---

## 11. Чек-лист безопасности перед запуском

- [ ] Доступ наружу — только через HTTPS (DSM Reverse Proxy + валидный сертификат).
- [ ] `X-Forwarded-For` проброшен, `TRUST_PROXY_HEADERS=true` — в журнале реальные IP.
- [ ] Порты `8000` (API) и `5432` (БД) **не** опубликованы наружу.
- [ ] `APP_ENV=prod` → Swagger (`/docs`) отключён.
- [ ] `AUTO_SEED=false` — демо-учёток с паролем `Demo1234!` в системе нет.
- [ ] Создан первый Владелец, пароль стойкий и хранится в сейфе (аварийный доступ).
- [ ] `POSTGRES_PASSWORD` изменён со значения по умолчанию (если порт БД когда-либо откроется).
- [ ] Том `secrets` в бэкапе — иначе при потере все сессии будут сброшены.
- [ ] Rate-limit входа включён (`LOGIN_RATE_LIMIT=10`).
- [ ] Настроены ежедневные бэкапы БД и Hyper Backup на тома.
- [ ] Проверено **восстановление** из бэкапа (тестовый прогон).
- [ ] Фаервол DSM: разрешены 443 и (для администрирования из доверенной сети) 22.
- [ ] DSM и Container Manager обновлены до актуальных версий.
- [ ] Сотрудники предупреждены: пароль Владельца не передавать, смену пароля делать в профиле.

---

## 12. Дальше

* 🚀 Быстрая установка: [`INSTALL.md`](INSTALL.md)
* 🔐 Вход через домен (необязательно): [`AD-OPTIONAL.md`](AD-OPTIONAL.md)
* 🏗 Устройство системы и ключевые решения: [`ARCHITECTURE.md`](ARCHITECTURE.md)
* 📡 Контракт API: [`API.md`](API.md)

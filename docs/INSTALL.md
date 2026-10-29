# Установка SchoolHub на Synology DS223 — быстрый старт

> Полная версия: 5 шагов, ~15 минут. Ни одного обязательного параметра:
> ключи шифрования генерируются сами, первого администратора вы создадите в браузере.
> **SSH не требуется** — всю установку можно выполнить мышкой через File Station и Container Manager.

**Что нужно:** NAS с DSM 7.2+, установленный **Container Manager**, ~2 ГБ места и File Station
(SSH — по желанию, для варианта со скриптом).

---

## Шаг 1. Скопируйте проект на NAS

**Вариант A — File Station (без терминала).**
1. Соберите папку `schoolhub` в ZIP (без `node_modules`, `dist`, `__pycache__` — они не нужны).
2. File Station → откройте `/docker` (если папки нет, создайте: **Панель управления → Общая папка**).
3. Загрузите ZIP в `/docker` → правый клик → **Извлечь** → получите `/docker/schoolhub`.
4. Переименуйте в `/docker/schoolhub/app` **или** оставьте как есть — скрипт установки сам разложит файлы.

**Вариант B — SSH + git** (удобнее для обновлений):

```bash
sudo -i
mkdir -p /volume1/docker/schoolhub && cd /volume1/docker/schoolhub
git clone <URL-вашего-репозитория> app
```

---

## Шаг 2. Запустите установку

### Вариант A — без SSH, мышкой (рекомендуется)

1. **Container Manager → Проект → Создать**.
2. Имя проекта: `schoolhub`; путь: `/docker/schoolhub/app` (папка из шага 1).
3. Файл compose: `deploy/synology/docker-compose.yml` → **Далее → Готово**.
4. Дождитесь окончания сборки: **первая сборка на DS223 занимает 5–12 минут**
   (ARM-процессор собирает фронтенд) — это нормально. Статус виден там же, в проекте.
5. **Код первоначальной настройки** посмотрите в журнале:
   **Container Manager → Контейнер → `schoolhub-backend` → вкладка «Журнал»** —
   найдите строку `Код первоначальной настройки: XXXXXXXX`.

### Вариант B — SSH + скрипт

```bash
sudo -i
cd /volume1/docker/schoolhub/app        # или туда, куда распаковали
sudo bash install-nas.sh
```

Скрипт проверит Docker, скопирует проект в `/volume1/docker/schoolhub/app`, соберёт образы
и запустит три контейнера. В конце он напечатает:

```
┌──────────────────────────────────────────────────────────────┐
│  Код первоначальной настройки:  VW88DT6V                      │
└──────────────────────────────────────────────────────────────┘
Откройте в браузере:   http://192.168.1.50:8080
```

<details>
<summary>Вручную через SSH, без скрипта</summary>

```bash
cd /volume1/docker/schoolhub/app
sudo docker compose -f deploy/synology/docker-compose.yml up -d --build
sudo docker logs schoolhub-backend | grep -i "код первоначальной"
```
</details>

---

## Шаг 3. Откройте веб-интерфейс

`http://<адрес-NAS>:8080`

Вы увидите страницу **«Первоначальная настройка»**.

---

## Шаг 4. Создайте Владельца

1. Введите **код настройки** из шага 2.
2. Укажите ФИО, e-mail (это будет логин), пароль и название школы.
3. Нажмите «Создать Владельца и войти».

Готово — вы внутри. Владелец видит всё: приказы, задачи всех сотрудников, пользователей, журнал событий.

<details>
<summary>Код не нашёлся / потерялся</summary>

**Без SSH:** Container Manager → Контейнер → `schoolhub-backend` → «Журнал» —
код печатается при каждом старте контейнера, пока настройка не завершена
(перезапустите контейнер, чтобы увидеть его снова).

**Через SSH:**

```bash
# посмотреть код
sudo docker exec -it schoolhub-backend python -m scripts.first_owner --show-token

# выпустить новый код
sudo docker exec -it schoolhub-backend python -m scripts.first_owner --reset-token

# или создать Владельца сразу из терминала (без браузера)
sudo docker exec -it schoolhub-backend python -m scripts.first_owner \
    --email admin@school.local --password 'ВашПароль123' --name 'Администратор школы'

# сбросить пароль существующему Владельцу
sudo docker exec -it schoolhub-backend python -m scripts.first_owner --force \
    --email admin@school.local --password 'НовыйПароль123'
```
</details>

---

## Шаг 5. Добавьте сотрудников

**Пользователи → «+ Локальная учётка»** — создайте завучей (роль *Администрация*) и учителей
(роль *Учитель*). Логин — e-mail, пароль задаёте вы и передаёте сотруднику. Если пароль
потерялся — сбросьте его там же, в карточке сотрудника («✎ Изменить»).

Дальше всё как в ТЗ:

* **Приказы** → «+ Новый приказ» → выберите **круг ознакомления** (все сотрудники или
  выбранный список) → загрузите PDF/DOCX → «Опубликовать».
  Сотрудники получат уведомление и нажмут «Ознакомлен» — в листе ознакомления появятся
  дата, время, IP и комментарий.
* **Задачи** → «+ Новая задача» → выберите **одного или нескольких исполнителей** и дедлайн,
  при необходимости прикрепите файлы. Каждый исполнитель ставит свою отметку «выполнено»;
  когда отметятся все — задача закроется сама. После дедлайна задача сама станет
  «Просрочена» (без планировщиков и cron).
* **План работы** → «+ Создать план» → заполните дежурство, блоки «в течение месяца/недели»
  и мероприятия по дням, назначьте ответственных из списка сотрудников → «Опубликовать».
  Ответственные получат уведомления, а любой сотрудник сможет распечатать план
  («🖨 Печать / PDF») — он выйдет в том же виде, что и Word-макет.

---

## HTTPS (необязательно, но рекомендуется)

1. **DSM → Панель управления → Безопасность → Сертификат** → Добавить → Let's Encrypt
   → имя `schoolhub.ваш-домен`.
2. **DSM → Панель управления → Вход в систему → Дополнительно → Обратный прокси** → Создать:
   * Источник: `HTTPS`, `schoolhub.ваш-домен`, порт `443`
   * Назначение: `HTTP`, `localhost`, порт `8080`
3. В правиле → **Пользовательская конфигурация** → вставьте
   [`deploy/synology/dsm-reverse-proxy.conf`](../deploy/synology/dsm-reverse-proxy.conf)
   (там проброс `X-Forwarded-For` — без него в журнале ознакомления не будет реального IP сотрудника).
4. DNS: A-запись `schoolhub` → IP вашего NAS.

---

## Бэкап

```bash
sudo bash /volume1/docker/schoolhub/backup-db.sh      # разовый дамп БД + sha256
```

**DSM → Планировщик задач → Создать → Скрипт, определённый пользователем**:
пользователь `root`, путь `/volume1/docker/schoolhub/backup-db.sh`, ежедневно в 03:30.

В **Hyper Backup** добавьте том с данными Docker (`/volume1/@docker`) — там лежат
тома `dbdata`, `uploads` и `secrets` (ключ шифрования).

Восстановление:

```bash
cd /volume1/docker/schoolhub/app
sudo docker compose -f deploy/synology/docker-compose.yml up -d db backend
gunzip -c /volume1/backup/schoolhub/schoolhub-XXXXXXXX.sql.gz | \
  sudo docker exec -i schoolhub-db psql -U schoolhub -d schoolhub
sudo docker compose -f deploy/synology/docker-compose.yml restart backend
```

---

## Обновление версии

```bash
cd /volume1/docker/schoolhub/app
git pull                                             # или заменить файлы из нового архива
sudo bash deploy/synology/backup-db.sh               # бэкап перед обновлением
sudo docker compose -f deploy/synology/docker-compose.yml up -d --build
sudo bash deploy/synology/check-health.sh            # диагностика
```

Структура БД обновляется автоматически при старте (идемпотентные миграции в `app/migrations.py`).

---

## Если что-то не так

Быстрая диагностика:

```bash
sudo bash /volume1/docker/schoolhub/app/deploy/synology/check-health.sh
```

| Симптом | Что сделать |
|---|---|
| Страница не открывается | `sudo docker ps` — все три контейнера `Up`? Порт занят: поменяйте `HTTP_PORT` в `.env` и перезапустите |
| «Система ещё не настроена» и нет кода | `docker logs schoolhub-backend` или `first_owner --show-token` |
| Ошибка 502 / белый экран | `docker logs schoolhub-frontend` и `docker logs schoolhub-backend` |
| Забыл пароль Владельца | `first_owner --force --email … --password …` (см. шаг 4) |
| Медленно работает | DS223 — 2 ГБ RAM: проверьте `docker stats`; лимиты уже заданы в compose |
| Не загружается PDF > 25 МБ | увеличьте `MAX_UPLOAD_MB` в `.env` и `MAX_UPLOAD_SIZE` (nginx) |
| Нужно посмотреть/скачать данные | `sudo docker exec -it schoolhub-db psql -U schoolhub -d schoolhub` |

Логи:

```bash
sudo docker logs -f schoolhub-backend
sudo docker logs -f schoolhub-frontend
sudo docker logs -f schoolhub-db
```

---

## Что можно настроить (необязательно)

Все параметры — в `deploy/synology/.env` (файл создаётся автоматически при установке):

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `HTTP_PORT` | `8080` | порт веб-интерфейса |
| `TZ` | `Europe/Moscow` | часовой пояс для дат в журналах |
| `MAX_UPLOAD_MB` | `25` | лимит размера документа приказа |
| `POSTGRES_PASSWORD` | `schoolhub` | пароль БД (наружу не публикуется) |
| `AUTO_SEED` | `false` | `true` — наполнить демо-данными (пароль `Demo1234!`) |
| `ACCESS_TOKEN_TTL_MIN` | `30` | время жизни сессии |
| `LDAP_ENABLED` | `false` | вход через домен — см. [`AD-OPTIONAL.md`](AD-OPTIONAL.md) |
| `SECRET_KEY` | генерируется | задавать не нужно; хранится в томе `secrets` |

---

## Ресурсы DS223 (2 ГБ RAM)

| Контейнер | Лимит | Реально потребляет |
|---|---|---|
| `schoolhub-db` | 420 МБ | 90–180 МБ |
| `schoolhub-backend` | 460 МБ | 130–220 МБ |
| `schoolhub-frontend` | 96 МБ | 8–15 МБ |
| **Итого** | **~980 МБ** | **~250–400 МБ** |

Остальное остаётся DSM. LibreOffice для превью DOCX намеренно **не** устанавливаем
(~400 МБ образа и тяжёлая конвертация): PDF просматривается в браузере, DOCX — скачивается.

---

## Дальше

* Вход через Active Directory (если понадобится): [`docs/AD-OPTIONAL.md`](AD-OPTIONAL.md)
* Устройство системы и ключевые решения: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)
* Полный контракт API: [`docs/API.md`](API.md)

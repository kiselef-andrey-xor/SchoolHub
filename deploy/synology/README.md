# Развёртывание SchoolHub на Synology DS223

🚀 **Быстрая установка (5 шагов): [`docs/INSTALL.md`](../../docs/INSTALL.md)**
📖 Справочник по эксплуатации: [`docs/DEPLOY-NAS.md`](../../docs/DEPLOY-NAS.md)
🔐 Вход через домен (необязательно): [`docs/AD-OPTIONAL.md`](../../docs/AD-OPTIONAL.md)

## Состав папки

| Файл | Назначение |
|---|---|
| `docker-compose.yml` | Проект Container Manager: PostgreSQL 16 + backend + nginx (arm64, лимиты памяти под 2 ГБ RAM). **Обязательных настроек нет** |
| `.env.nas.example` | Необязательные настройки (порт, часовой пояс, лимиты, AD) |
| `dsm-reverse-proxy.conf` | Пользовательская конфигурация обратного прокси DSM (HTTPS → контейнер) |
| `setup-ad.ps1` | Подготовка домена — нужна только если включаете вход через AD |
| `backup-db.sh` | Ежедневный `pg_dump` в общую папку NAS (для Hyper Backup / Snapshot Replication) |
| `check-health.sh` | Быстрая диагностика контейнеров, БД, LDAP и дискового места по SSH |

## Коротко

```bash
# по SSH на NAS: скопируйте проект в /volume1/docker/schoolhub/app, затем
sudo bash /volume1/docker/schoolhub/app/install-nas.sh
```

Скрипт соберёт образы, запустит стек и напечатает **код первоначальной настройки**.
Дальше откройте `http://<адрес-NAS>:8080` и создайте Владельца в браузере.

Вручную:

```bash
cd /volume1/docker/schoolhub/app
sudo docker compose -f deploy/synology/docker-compose.yml up -d --build
sudo docker logs schoolhub-backend | grep -i "код первоначальной"
```

HTTPS: DSM → Обратный прокси `https://schoolhub.…` → `http://localhost:8080`
(вставить `dsm-reverse-proxy.conf`).

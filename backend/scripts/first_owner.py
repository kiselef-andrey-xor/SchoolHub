"""Создание первого Владельца из терминала — альтернатива веб-странице настройки.

Запуск на Synology (в Container Manager или по SSH):

    docker exec -it schoolhub-backend python -m scripts.first_owner \
        --email admin@school.local --password 'ВашПароль' --name 'Администратор школы'

Скрипт идемпотентен: если Владелец уже есть, ничего не меняет.
Флаги --reset-token и --show-token помогают, если код настройки потерялся.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.enums import Role  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import bootstrap  # noqa: E402
from app.services.bootstrap import OwnerCredentials  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    await init_db()

    async with SessionLocal() as session:
        if args.show_token:
            state = await bootstrap.get_setup_state(session)
            await session.commit()
            print(f"Код первоначальной настройки: {state.setup_token}")
            return 0

        if args.reset_token:
            import secrets

            state = await bootstrap.get_setup_state(session)
            state.setup_token = args.reset_token if args.reset_token != "-" else secrets.token_urlsafe(6)[:8].upper()
            await session.commit()
            print(f"Новый код настройки: {state.setup_token}")
            return 0

        if not args.email or not args.password:
            print("Укажите --email и --password (или --show-token, чтобы настроить через браузер)")
            return 2

        owners = int(
            (
                await session.execute(
                    select(func.count()).select_from(User).where(User.role == Role.OWNER, User.is_active.is_(True))
                )
            ).scalar_one()
        )
        if owners > 0 and not args.force:
            print(f"В системе уже есть активных Владельцев: {owners}. Ничего не меняю (нужен --force).")
            return 0

        owner = await bootstrap.create_first_owner(
            session,
            OwnerCredentials(
                email=args.email,
                password=args.password,
                full_name=args.name or "Администратор",
                school_name=args.school,
            ),
            token=None,                    # из терминала код не требуется: доступ к контейнеру = доступ к админу
        )
        await session.commit()

    print()
    print("✅ Готово. Учётная запись Владельца создана:")
    print(f"   e-mail:  {owner.email}")
    print("   пароль:  (тот, что вы указали)")
    print()
    print("   Откройте веб-интерфейс и войдите под этими данными.")
    print("   После входа смените пароль в разделе «Мой профиль», если пароль передавался через историю команд.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Первоначальная настройка SchoolHub")
    parser.add_argument("--email", help="e-mail первого Владельца")
    parser.add_argument("--password", help="пароль (минимум 8 символов)")
    parser.add_argument("--name", help="ФИО, например 'Администратор школы'")
    parser.add_argument("--school", help="название школы (необязательно)")
    parser.add_argument("--show-token", action="store_true", help="показать код настройки для веб-формы")
    parser.add_argument("--reset-token", nargs="?", const="-", help="выпустить новый код настройки")
    parser.add_argument("--force", action="store_true", help="создать Владельца, даже если он уже есть")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

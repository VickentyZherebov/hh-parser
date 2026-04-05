"""
CLI для управления HH-Parser: создание БД, пользователей.

Использование:
    python manage.py init-db
    python manage.py create-user --login admin --name "Викентий" --password secret --role admin
    python manage.py create-user --login masha --name "Маша Истомина" --password secret
    python manage.py list-users
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from web.db import init_db, create_user, list_users, get_user_by_login
from web.auth import hash_password


def cmd_init_db(args):
    init_db()
    print("БД инициализирована.")


def cmd_create_user(args):
    init_db()

    if get_user_by_login(args.login):
        print(f"Пользователь '{args.login}' уже существует.")
        sys.exit(1)

    pw_hash = hash_password(args.password)
    user_id = create_user(args.login, pw_hash, args.name, args.role)
    print(f"Создан пользователь: {args.login} (id={user_id}, role={args.role})")


def cmd_list_users(args):
    init_db()
    users = list_users()
    if not users:
        print("Пользователей нет.")
        return
    print(f"{'ID':<5} {'Логин':<15} {'Имя':<25} {'Роль':<8} {'Создан'}")
    print("-" * 70)
    for u in users:
        print(f"{u['id']:<5} {u['login']:<15} {u['display_name']:<25} {u['role']:<8} {u['created_at']}")


def main():
    parser = argparse.ArgumentParser(description="HH-Parser Management CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-db", help="Инициализировать БД")

    p_create = sub.add_parser("create-user", help="Создать пользователя")
    p_create.add_argument("--login", required=True)
    p_create.add_argument("--name", required=True, help="Отображаемое имя")
    p_create.add_argument("--password", required=True)
    p_create.add_argument("--role", default="user", choices=["user", "admin"])

    sub.add_parser("list-users", help="Список пользователей")

    args = parser.parse_args()

    if args.command == "init-db":
        cmd_init_db(args)
    elif args.command == "create-user":
        cmd_create_user(args)
    elif args.command == "list-users":
        cmd_list_users(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

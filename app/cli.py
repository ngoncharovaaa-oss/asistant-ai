# -*- coding: utf-8 -*-
"""Командная строка AsiStantAI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .journal import Journal
from .pipeline import run

PERIOD_CHOICES = ["today", "3d", "7d", "30d", "all"]


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.load()
    if getattr(args, "data_dir", None):
        settings.data_dir = Path(args.data_dir).resolve()
    if getattr(args, "out_dir", None):
        settings.out_dir = Path(args.out_dir).resolve()
    if getattr(args, "db", None):
        settings.db_path = Path(args.db).resolve()
    return settings


def _print_run(result) -> None:
    metrics = result.metrics
    accuracy = metrics["accuracy"]
    print(f"AsiStantAI · {result.profile.name} · запуск #{result.run_id}")
    print(f"Писем: {metrics['emails']} · черновиков: {metrics['drafts']} · автоотправок: {metrics['auto_sends']}")
    print(f"Точность разбора: {accuracy['hits']}/{accuracy['total']} ({accuracy['accuracy']:.1%})")
    print(
        "Действия: "
        + " · ".join(f"{key} {value}" for key, value in metrics["actions"].items())
    )
    print(
        f"Протоколов: {metrics['protocols']} · поручений: {metrics['protocol_tasks']} · "
        f"открытых вопросов: {metrics['protocol_open_questions']}"
    )
    print(f"Отчёт: {result.files['report_md']}")
    print(f"Дашборд: python -m app.cli serve")


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    profile = Path(args.profile).resolve() if args.profile else None
    if getattr(args, "live", False):
        from .live import ChannelError, run_live

        journal = Journal(settings.db_path)
        try:
            result = run_live(settings, journal, profile_path=profile)
        except ChannelError as exc:
            print(f"Живой прогон невозможен: {exc}")
            return 1
        finally:
            journal.close()
        if result is None:
            print("Новых писем нет — очередь не менялась.")
            return 0
        if args.json:
            print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
        else:
            _print_run(result)
        return 0
    result = run(settings, profile_path=profile)
    if args.json:
        print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
    else:
        _print_run(result)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = _settings(args)
    journal = Journal(settings.db_path)
    try:
        metrics = journal.run_metrics()
    finally:
        journal.close()
    if not metrics:
        print("Запусков ещё нет: python -m app.cli run")
        return 1
    path = settings.out_dir / "report.md"
    if path.exists():
        print(path.read_text(encoding="utf-8"))
    else:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("app.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_protocols(args: argparse.Namespace) -> int:
    from .meetings import parse_all, write_protocols

    settings = _settings(args)
    protocols = parse_all(settings.data_dir / "meetings")
    write_protocols(protocols, settings.out_dir / "protocols")
    for protocol in protocols:
        print(f"{protocol.id} · {protocol.title} · решений {len(protocol.decisions)} · "
              f"поручений {len(protocol.tasks)} · вопросов {len(protocol.open_questions)}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from .live import ChannelError, approve_draft

    settings = _settings(args)
    journal = Journal(settings.db_path)
    try:
        result = approve_draft(settings, journal, args.draft_id, args.comment)
    except ChannelError as exc:
        result = {"ok": False, "error": str(exc)}
    finally:
        journal.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_channels(args: argparse.Namespace) -> int:
    from .live import channels_status

    settings = _settings(args)
    status = channels_status(settings, check=args.check)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    for label, account in status["accounts"].items():
        name = "А" if label == "a" else "Б"
        if not account.get("configured"):
            print(f"Ящик {name}: не настроен")
            continue
        detail = account.get("detail", "настроен")
        smtp = ""
        if "smtp_ok" in account:
            smtp = " · SMTP ок" if account["smtp_ok"] else " · SMTP ошибка"
        print(f"Ящик {name} · {account.get('address', '')}: {detail}{smtp}")
    telegram = status["telegram"]
    if not telegram.get("configured"):
        print("Telegram: не настроен")
    else:
        detail = telegram.get("detail") or ("бот @" + telegram["username"] if telegram.get("username") else "токен задан")
        print(f"Telegram: {detail}")
    tg_user = status.get("telegram_user", {})
    if tg_user.get("configured"):
        if tg_user.get("authorized"):
            print("Мой Telegram: вход выполнен" + (f": @{tg_user['username']}" if tg_user.get("username") else ""))
        else:
            print("Мой Telegram: сессия не авторизована — python -m app.cli tg-login")
    zoom = status.get("zoom", {})
    if not zoom.get("configured"):
        print("Zoom: не настроен")
    elif zoom.get("mode") == "login":
        name = zoom.get("name")
        print("Zoom · вход выполнен" + (f": {name}" if name else ""))
    else:
        mode = "API" if zoom.get("mode") == "api" else "постоянная ссылка"
        detail = zoom.get("detail")
        print(f"Zoom · {mode}" + (f": {detail}" if detail else ""))
    telemost = status.get("telemost", {})
    if telemost.get("invalid"):
        print("Телемост: ссылка должна быть на telemost.yandex.ru")
    elif not telemost.get("configured"):
        print("Телемост: не настроен")
    else:
        detail = telemost.get("detail")
        print("Телемост · постоянная комната" + (f": {detail}" if detail else ""))
    bitrix = status.get("bitrix24", {})
    if bitrix.get("invalid"):
        print("Битрикс24: адрес вебхука должен быть https://<портал>/rest/<id>/<код>/")
    elif not bitrix.get("configured"):
        print("Битрикс24: не настроен")
    else:
        detail = bitrix.get("detail")
        print("Битрикс24 · " + (bitrix.get("portal") or "портал") + (f": {detail}" if detail else ""))
    print("Живой режим: " + ("готов" if status["live_ready"] else "нужны почта и Telegram в .env"))
    return 0


def cmd_mailboxes(args: argparse.Namespace) -> int:
    from .live import mailboxes_overview

    settings = _settings(args)
    data = mailboxes_overview(settings, with_unread=not args.no_check, period=args.period)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if not data["mailboxes"]:
        print("Ящиков нет: добавьте в дашборде (панель «Почты») или POST /api/mailboxes.")
        return 0
    for item in data["mailboxes"]:
        if not item.get("configured"):
            print(f"{item['address']}: не настроен — нужен пароль приложения")
        elif item.get("unread_error"):
            print(f"{item['address']}: ошибка — {item['unread_error']}")
        elif item.get("unread") is None:
            print(f"{item['address']}: настроен")
        else:
            print(f"{item['address']}: непрочитанных {data['period_title']} {item['unread']}, сегодня {item.get('unread_today') or 0}")
    print(f"Всего непрочитанных {data['period_title']}: {data['unread']} · сегодня: {data['unread_today']}")
    return 0


def cmd_zoom_link(args: argparse.Namespace) -> int:
    from .live import Channels
    from .zoom import ZoomError

    settings = _settings(args)
    channels = Channels(settings)
    try:
        result = channels.zoom.create_meeting(args.topic, args.start, args.minutes, args.agenda)
    except ZoomError as exc:
        print(f"Zoom: {exc}")
        return 1
    journal = Journal(settings.db_path)
    try:
        journal.log("zoom_link", {"topic": result.get("topic", ""), "mode": result.get("mode", "")})
    finally:
        journal.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Тема: {result['topic']}")
        if result.get("mode") == "api":
            print(f"Встреча создана · id {result.get('id') or '—'} · {result.get('minutes', 0)} мин")
        print(f"Ссылка: {result['join_url']}")
    return 0


def cmd_zoom_login_url(args: argparse.Namespace) -> int:
    from .live import Channels
    from .zoom import ZoomError, authorize_url

    settings = _settings(args)
    try:
        url = authorize_url(Channels(settings).zoom.config)
    except ZoomError as exc:
        print(f"Zoom: {exc}")
        return 1
    if args.json:
        print(json.dumps({"url": url}, ensure_ascii=False, indent=2))
    else:
        print("Откройте ссылку в браузере и войдите в Zoom:")
        print(url)
    return 0


def cmd_zoom_logout(args: argparse.Namespace) -> int:
    from .live import Channels

    settings = _settings(args)
    Channels(settings).zoom.logout()
    print("Zoom: вход завершён, токены удалены")
    return 0


def cmd_set_link(args: argparse.Namespace) -> int:
    from .links import LinkError, set_link

    settings = _settings(args)
    try:
        value = set_link(settings, args.provider, args.url)
    except LinkError as exc:
        print(f"Ссылка: {exc}")
        return 1
    print(("Ссылка сохранена: " if value else "Ссылка убрана: ") + args.provider)
    return 0


def cmd_telemost_link(args: argparse.Namespace) -> int:
    from .live import Channels
    from .telemost import TelemostError

    settings = _settings(args)
    channels = Channels(settings)
    try:
        result = channels.telemost.create_meeting(args.topic, args.start)
    except TelemostError as exc:
        print(f"Телемост: {exc}")
        return 1
    journal = Journal(settings.db_path)
    try:
        journal.log("telemost_link", {"topic": result.get("topic", ""), "mode": result.get("mode", "")})
    finally:
        journal.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Тема: {result['topic']}")
        if result.get("start"):
            print(f"Когда: {result['start'].replace('T', ' ')}")
        print(f"Ссылка: {result['join_url']}")
        print("Приглашение:")
        print(result["invite"])
    return 0


def cmd_tg_login(args: argparse.Namespace) -> int:
    from .tg_user import TelegramUserError, login

    settings = _settings(args)
    try:
        result = login(settings, phone=args.phone)
    except TelegramUserError as exc:
        print(f"Telegram: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Вход выполнен" + (f": @{result['username']}" if result.get("username") else ""))
        print("Сессия сохранена локально и в git не попадает.")
    return 0


def cmd_tg_unread(args: argparse.Namespace) -> int:
    from .live import Channels
    from .tg_user import TelegramUserError

    settings = _settings(args)
    try:
        data = Channels(settings).tg_user.unread(args.limit, args.period)
    except TelegramUserError as exc:
        print(f"Telegram: {exc}")
        return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"Непрочитанных {data['period_title']}: {data['total']} · чатов: {data['dialogs']}")
    for item in data["items"]:
        name = item["name"] or (f"@{item['username']}" if item["username"] else "Чат")
        tail = f" · {item['last_text']}" if item["last_text"] else ""
        print(f"- {name}: {item['unread']}{tail}")
    return 0


def cmd_tg_drafts(args: argparse.Namespace) -> int:
    from .live import Channels
    from .tg_user import TelegramUserError

    settings = _settings(args)
    try:
        data = Channels(settings).tg_user.drafts(args.limit, args.period)
    except TelegramUserError as exc:
        print(f"Telegram: {exc}")
        return 1
    journal = Journal(settings.db_path)
    try:
        journal.log("tg_drafts", {"dialogs": data["dialogs"], "drafts": len(data["drafts"])})
    finally:
        journal.close()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"Черновиков: {len(data['drafts'])} · непрочитанных {data['period_title']}: {data['total']}")
    for draft in data["drafts"]:
        print(f"\n{draft['name'] or 'Чат'} ({draft['unread']} непрочит.)")
        if draft["basis"]:
            print(f"Сообщение: {draft['basis']}")
        print(draft["reply"])
    return 0


def cmd_tg_listen(args: argparse.Namespace) -> int:
    from .live import ChannelError, telegram_listen

    settings = _settings(args)
    journal = Journal(settings.db_path)
    try:
        print("Слушаю Telegram. Ctrl+C — остановить.")
        telegram_listen(settings, journal, once=args.once)
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except ChannelError as exc:
        print(f"Telegram: {exc}")
        return 1
    finally:
        journal.close()
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    settings = _settings(args)
    body = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else args.text
    if not body:
        print("Нужен --text или --text-file")
        return 2
    journal = Journal(settings.db_path)
    try:
        result = journal.save_edit(args.draft_id, body, args.comment)
    finally:
        journal.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asistant", description="AsiStantAI — ИИ-помощник руководителя")
    parser.add_argument("--data-dir", help="папка датасета")
    parser.add_argument("--out-dir", help="папка результатов")
    parser.add_argument("--db", help="путь к SQLite-журналу")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="разобрать почту и собрать черновики")
    run_parser.add_argument("--profile", help="путь к профилю руководителя")
    run_parser.add_argument("--live", action="store_true", help="взять новые письма из IMAP")
    run_parser.add_argument("--json", action="store_true", help="напечатать метрики в JSON")
    run_parser.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="показать последний отчёт")
    report.set_defaults(func=cmd_report)

    serve = sub.add_parser("serve", help="поднять дашборд")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    protocols = sub.add_parser("protocols", help="только протоколы встреч")
    protocols.set_defaults(func=cmd_protocols)

    approve = sub.add_parser("approve", help="утвердить черновик (демо-отправка)")
    approve.add_argument("draft_id")
    approve.add_argument("--comment", default="")
    approve.set_defaults(func=cmd_approve)

    edit = sub.add_parser("edit", help="поправить черновик — тема запомнит стиль")
    edit.add_argument("draft_id")
    edit.add_argument("--text", default="")
    edit.add_argument("--text-file", default="")
    edit.add_argument("--comment", default="")
    edit.set_defaults(func=cmd_edit)

    channels = sub.add_parser("channels", help="статус почты, Telegram, Zoom и Телемоста")
    channels.add_argument("--check", action="store_true", help="проверить IMAP/SMTP, бота, Zoom и Телемост")
    channels.add_argument("--json", action="store_true")
    channels.set_defaults(func=cmd_channels)

    mailboxes = sub.add_parser("mailboxes", help="ящики и непрочитанные за период")
    mailboxes.add_argument("--no-check", action="store_true", help="без входа в IMAP: только состав реестра")
    mailboxes.add_argument("--period", choices=PERIOD_CHOICES, default="all", help="окно непрочитанных")
    mailboxes.add_argument("--json", action="store_true")
    mailboxes.set_defaults(func=cmd_mailboxes)

    zoom = sub.add_parser("zoom-link", help="создать ссылку Zoom на встречу")
    zoom.add_argument("--topic", required=True, help="тема встречи")
    zoom.add_argument("--start", default="", help="когда: 2026-11-28 10:00")
    zoom.add_argument("--minutes", type=int, default=45)
    zoom.add_argument("--agenda", default="")
    zoom.add_argument("--json", action="store_true")
    zoom.set_defaults(func=cmd_zoom_link)

    telemost = sub.add_parser("telemost-link", help="ссылка Телемоста на встречу")
    telemost.add_argument("--topic", required=True, help="тема встречи")
    telemost.add_argument("--start", default="", help="когда: 2026-11-28 10:00")
    telemost.add_argument("--json", action="store_true")
    telemost.set_defaults(func=cmd_telemost_link)

    zoom_login = sub.add_parser("zoom-login-url", help="ссылка для входа в Zoom через OAuth-приложение")
    zoom_login.add_argument("--json", action="store_true")
    zoom_login.set_defaults(func=cmd_zoom_login_url)

    zoom_logout = sub.add_parser("zoom-logout", help="выйти из Zoom и удалить токены")
    zoom_logout.set_defaults(func=cmd_zoom_logout)

    set_link = sub.add_parser("set-link", help="сохранить постоянную ссылку на комнату")
    set_link.add_argument("--provider", required=True, choices=["zoom", "telemost"])
    set_link.add_argument("--url", required=True, help="https://zoom.us/j/… или https://telemost.yandex.ru/j/…")
    set_link.set_defaults(func=cmd_set_link)

    tg = sub.add_parser("tg-listen", help="слушать команды Telegram-бота")
    tg.add_argument("--once", action="store_true", help="обработать один пакет и выйти")
    tg.set_defaults(func=cmd_tg_listen)

    tg_login = sub.add_parser("tg-login", help="вход в мой Telegram (код приходит в приложение)")
    tg_login.add_argument("--phone", default="", help="номер в формате +7…, если не задан в .env")
    tg_login.add_argument("--json", action="store_true")
    tg_login.set_defaults(func=cmd_tg_login)

    tg_unread = sub.add_parser("tg-unread", help="непрочитанные в моём Telegram за период")
    tg_unread.add_argument("--limit", type=int, default=20)
    tg_unread.add_argument("--period", choices=PERIOD_CHOICES, default="all", help="окно непрочитанных")
    tg_unread.add_argument("--json", action="store_true")
    tg_unread.set_defaults(func=cmd_tg_unread)

    tg_drafts = sub.add_parser("tg-drafts", help="черновики ответов на непрочитанные в Telegram")
    tg_drafts.add_argument("--limit", type=int, default=5)
    tg_drafts.add_argument("--period", choices=PERIOD_CHOICES, default="all", help="окно непрочитанных")
    tg_drafts.add_argument("--json", action="store_true")
    tg_drafts.set_defaults(func=cmd_tg_drafts)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

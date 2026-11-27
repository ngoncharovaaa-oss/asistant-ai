# -*- coding: utf-8 -*-
"""Личный чат руководителя: сотрудники пишут напрямую, Ася готовит черновики.

Сообщения хранятся локально в SQLite рядом с журналом. Сотрудник может
написать через панель дашборда, а в рабочем контуре — через Telegram-бота.
Непрочитанные считаются по тому же окну периодов, что и почта с Telegram.
Отправка ответов — только вручную: 0 автоотправок.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .periods import normalize_period, period_cutoff, period_title

SCHEMA = """
CREATE TABLE IF NOT EXISTS manager_chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee TEXT NOT NULL,
  role TEXT DEFAULT '',
  direction TEXT NOT NULL DEFAULT 'in',
  text TEXT NOT NULL,
  source TEXT DEFAULT 'dashboard',
  at TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0,
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_manager_chat_at ON manager_chat(at);
"""

EMPLOYEES: list[dict[str, str]] = [
    {"name": "Ольга Смирнова", "position": "Директор по продажам SMB", "role": "подчинённый"},
    {"name": "Дмитрий Козлов", "position": "Руководитель digital-маркетинга", "role": "подчинённый"},
    {"name": "Анна Волкова", "position": "Руководитель e-commerce", "role": "подчинённый"},
    {"name": "Сергей Морозов", "position": "Руководитель партнёрского канала", "role": "подчинённый"},
    {"name": "Мария Лебедева", "position": "Руководитель клиентского сервиса", "role": "подчинённый"},
    {"name": "Алексей Новиков", "position": "Аналитик данных", "role": "подчинённый"},
    {"name": "Екатерина Соколова", "position": "Ассистент направления", "role": "подчинённый"},
    {"name": "Наталья Павлова", "position": "Финансовый контролёр", "role": "коллега"},
    {"name": "Игорь Фёдоров", "position": "HR-бизнес-партнёр", "role": "коллега"},
    {"name": "Павел Егоров", "position": "Директор по ИТ", "role": "коллега"},
]
_ROLES = {item["name"]: item for item in EMPLOYEES}


class ChatError(RuntimeError):
    """Ошибка личного чата без секретов в тексте."""


def _first_name(name: str) -> str:
    parts = (name or "").split()
    return parts[0] if parts else ""


def reply_draft(employee: str, text: str) -> tuple[str, str, str]:
    """Черновик ответа и причина: шаблоны по смыслу сообщения, без автоотправки."""
    first = _first_name(employee)
    greeting = f"{first}, привет!" if first else "Привет!"
    low = (text or "").lower()
    if any(word in low for word in ("согласу", "оффер", "отпуск", "скидк", "бюджет", "контракт", "подтверд")):
        body = "Принято — посмотрю и вернусь с решением до конца дня."
        kind = "approval"
        reason = "Решение по деньгам, кадрам или срокам остаётся за руководителем."
    elif any(word in low for word in ("прошу", "нужно", "подготов", "сделай", "проверь", "согласуй")):
        body = "Принято, беру в работу — вернусь с результатом."
        kind = "task"
        reason = "Поручение сотруднику: подтверждение без обязательств по срокам."
    elif any(word in low for word in ("отчёт", "отчет", "прогноз", "план", "итог", "цифр")):
        body = "Спасибо, данные учту в сводке."
        kind = "report"
        reason = "Отчёт без вопроса: достаточно подтверждения."
    elif any(word in low for word in ("спасибо", "благодар", "поздрав")):
        body = "Спасибо!"
        kind = "thanks"
        reason = "Вежливость без решения."
    else:
        body = "Принято, отвечу в ближайшее время."
        kind = "info"
        reason = "Нейтральный ответ, детали — после уточнения."
    return f"{greeting} {body}", kind, reason


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ChatStore:
    def __init__(self, path: Path, seed: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if seed and not self._count():
            self._seed()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM manager_chat").fetchone()
        return int(row["n"] if row else 0)

    def _seed(self) -> None:
        now = datetime.now()

        def at(days: int, hours: int, minutes: int) -> str:
            moment = (now - timedelta(days=days)).replace(
                hour=hours, minute=minutes, second=0, microsecond=0
            )
            if moment > now:
                moment -= timedelta(days=1)
            return moment.isoformat(timespec="seconds")

        rows = [
            ("Ольга Смирнова", "in", "Иван, клиент «Ромашка-Ритейл» просит 12% на годовой контракт 4,8 млн ₽. Маржа остаётся 18%. Согласуешь до четверга?", at(0, 9, 12), 0),
            ("Анна Волкова", "in", "Статус e-commerce перенесла на среду 11:00 — слот был свободен, участники уведомлены.", at(0, 8, 40), 0),
            ("Мария Лебедева", "in", "Клиент «ТехноСтрой» недоволен сроками ответа поддержки. Предлагаю добавить разбор в повестку планёрки.", at(0, 10, 5), 0),
            ("Игорь Фёдоров", "in", "Финалист Сергей К. готов к офферу, нужен ваш ответ до пятницы.", at(0, 8, 55), 0),
            ("Дмитрий Козлов", "in", "Собрал итоги недели по digital: план 96%, два лида перенесли старт. Отчёт в папке.", at(1, 17, 20), 0),
            ("Ольга Смирнова", "in", "Иван, по скидке жду решение — клиент спрашивает повторно.", at(1, 16, 40), 0),
            ("Сергей Морозов", "in", "Партнёр просит обучение для их команды до 15 декабря. Подготовлю план, если согласуете.", at(4, 12, 10), 0),
            ("Екатерина Соколова", "in", "Напоминаю: стратегическая сессия 4 декабря, зал забронирован.", at(12, 16, 30), 0),
            ("Ольга Смирнова", "out", "Ольга, по скидке посмотрю и вернусь до четверга.", at(1, 18, 2), 1),
        ]
        for employee, direction, text, moment, read in rows:
            role = _ROLES.get(employee, {}).get("role", "")
            self.conn.execute(
                "INSERT INTO manager_chat(employee, role, direction, text, source, at, read)"
                " VALUES(?,?,?,?,?,?,?)",
                (employee, role, direction, text, "demo", moment, read),
            )

    def _rows(self, period: str, employee: str = "") -> list[dict[str, Any]]:
        cutoff = period_cutoff(period)
        sql = "SELECT * FROM manager_chat"
        where: list[str] = []
        args: list[Any] = []
        if cutoff is not None:
            where.append("at >= ?")
            args.append(cutoff.strftime("%Y-%m-%dT%H:%M:%S"))
        if employee:
            where.append("employee = ? COLLATE NOCASE")
            args.append(employee)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY at ASC, id ASC"
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def threads(self, period: str = "all") -> dict[str, Any]:
        period = normalize_period(period)
        rows = self._rows(period)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["employee"], []).append(row)
        threads: list[dict[str, Any]] = []
        unread_total = 0
        for employee, items in groups.items():
            unread = sum(1 for item in items if item["direction"] == "in" and not item["read"])
            unread_total += unread
            last = items[-1]
            threads.append(
                {
                    "employee": employee,
                    "role": last.get("role") or _ROLES.get(employee, {}).get("role", ""),
                    "position": _ROLES.get(employee, {}).get("position", ""),
                    "unread": unread,
                    "total": len(items),
                    "last_text": last["text"],
                    "last_at": last["at"],
                    "last_direction": last["direction"],
                    "demo": all(item.get("source") == "demo" for item in items),
                }
            )
        threads.sort(key=lambda item: item["last_at"], reverse=True)
        return {
            "period": period,
            "period_title": period_title(period),
            "unread": unread_total,
            "threads": threads,
            "employees": EMPLOYEES,
            "checked_at": _now_iso(),
        }

    def thread(self, employee: str, period: str = "all", limit: int = 100) -> dict[str, Any]:
        name = (employee or "").strip()
        if not name:
            raise ChatError("Нужно имя сотрудника")
        period = normalize_period(period)
        rows = self._rows(period, name)
        if not rows:
            return {
                "employee": name,
                "role": _ROLES.get(name, {}).get("role", ""),
                "period": period,
                "period_title": period_title(period),
                "unread": 0,
                "messages": [],
                "draft": None,
                "checked_at": _now_iso(),
            }
        messages = rows[-max(1, min(limit, 200)):]
        unread = [item for item in rows if item["direction"] == "in" and not item["read"]]
        draft = None
        if unread:
            latest = unread[-1]
            reply, kind, reason = reply_draft(name, latest["text"])
            draft = {
                "employee": name,
                "basis": latest["text"],
                "reply": reply,
                "kind": kind,
                "reason": reason,
                "note": "Черновик Аси · отправка вручную, 0 автоотправок.",
            }
        last = rows[-1]
        return {
            "employee": name,
            "role": last.get("role") or _ROLES.get(name, {}).get("role", ""),
            "period": period,
            "period_title": period_title(period),
            "unread": len(unread),
            "messages": [
                {
                    "id": item["id"],
                    "direction": item["direction"],
                    "text": item["text"],
                    "at": item["at"],
                    "read": bool(item["read"]),
                    "source": item.get("source", ""),
                }
                for item in messages
            ],
            "draft": draft,
            "checked_at": _now_iso(),
        }

    def add(
        self,
        employee: str,
        text: str,
        role: str = "",
        source: str = "dashboard",
        at: str = "",
    ) -> dict[str, Any]:
        name = " ".join((employee or "").split())
        body = (text or "").strip()
        if not name:
            raise ChatError("Нужно имя сотрудника")
        if not body:
            raise ChatError("Сообщение пустое")
        if len(body) > 2000:
            raise ChatError("Сообщение больше 2000 символов")
        role = (role or "").strip() or _ROLES.get(name, {}).get("role", "")
        cur = self.conn.execute(
            "INSERT INTO manager_chat(employee, role, direction, text, source, at, read)"
            " VALUES(?,?,?,?,?,?,0)",
            (name, role, "in", body, (source or "dashboard")[:40], at or _now_iso()),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM manager_chat WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def reply(self, employee: str, text: str) -> dict[str, Any]:
        name = " ".join((employee or "").split())
        body = (text or "").strip()
        if not name:
            raise ChatError("Нужно имя сотрудника")
        if not body:
            raise ChatError("Ответ пустой")
        if len(body) > 2000:
            raise ChatError("Ответ больше 2000 символов")
        role = _ROLES.get(name, {}).get("role", "")
        moment = _now_iso()
        cur = self.conn.execute(
            "INSERT INTO manager_chat(employee, role, direction, text, source, at, read)"
            " VALUES(?,?,?,?,?,?,1)",
            (name, role, "out", body, "manager", moment),
        )
        self.conn.execute(
            "UPDATE manager_chat SET read = 1, read_at = ?"
            " WHERE employee = ? COLLATE NOCASE AND direction = 'in' AND read = 0",
            (moment, name),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM manager_chat WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def mark_read(self, employee: str = "") -> int:
        moment = _now_iso()
        if employee:
            cur = self.conn.execute(
                "UPDATE manager_chat SET read = 1, read_at = ?"
                " WHERE employee = ? COLLATE NOCASE AND direction = 'in' AND read = 0",
                (moment, employee),
            )
        else:
            cur = self.conn.execute(
                "UPDATE manager_chat SET read = 1, read_at = ?"
                " WHERE direction = 'in' AND read = 0",
                (moment,),
            )
        self.conn.commit()
        return int(cur.rowcount or 0)

    def drafts(self, period: str = "all", employee: str = "") -> dict[str, Any]:
        period = normalize_period(period)
        name = (employee or "").strip()
        rows = self._rows(period, name)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["employee"], []).append(row)
        drafts: list[dict[str, Any]] = []
        unread_total = 0
        for who, items in groups.items():
            unread = [item for item in items if item["direction"] == "in" and not item["read"]]
            if not unread:
                continue
            unread_total += len(unread)
            latest = unread[-1]
            reply, kind, reason = reply_draft(who, latest["text"])
            drafts.append(
                {
                    "employee": who,
                    "role": _ROLES.get(who, {}).get("role", ""),
                    "unread": len(unread),
                    "basis": latest["text"],
                    "reply": reply,
                    "kind": kind,
                    "reason": reason,
                    "note": "Черновик Аси · отправка вручную, 0 автоотправок.",
                }
            )
        drafts.sort(key=lambda item: item["unread"], reverse=True)
        return {
            "total": unread_total,
            "drafts": drafts,
            "period": period,
            "period_title": period_title(period),
            "checked_at": _now_iso(),
        }

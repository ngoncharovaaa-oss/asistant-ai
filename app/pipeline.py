# -*- coding: utf-8 -*-
"""Полный проход: данные → разбор → проверка правил → черновики → протоколы → сводки → журнал."""
from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import economics as econ
from .classify import classify
from .compose import build_draft
from .config import Settings
from .datasets import load_calendar, load_contacts, load_inbox, load_org, load_tasks
from .digest import digest_markdown, morning_plan, weekly_digest
from .journal import Journal
from .knowledge import KnowledgeBase
from .meetings import parse_all, write_protocols
from .models import SENSITIVE_RISKS, Action, Classification, Draft, Email
from .profile import Profile, load_profile
from .rules import audit as rules_audit


@dataclass
class RunResult:
    run_id: int
    profile: Profile
    emails: list[Email]
    classifications: list[Classification]
    drafts: list[Draft]
    protocols: list
    morning: dict
    digest: dict
    metrics: dict
    economics: dict
    out_dir: Path
    files: dict[str, str] = field(default_factory=dict)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _enforce_safety(email: Email, item: Classification, threshold: float) -> Classification:
    if set(item.risks) & SENSITIVE_RISKS and item.action != Action.ASK:
        item.reasons.append("страховка: чувствительная тема всегда уходит на подтверждение")
        item.action = Action.ASK
    if item.action != Action.ASK and item.confidence < threshold:
        item.reasons.append(
            f"уверенность {item.confidence:.2f} ниже порога {threshold:.2f}: решает человек"
        )
        item.action = Action.ASK
    return item


def _accuracy(emails: list[Email], classifications: list[Classification]) -> dict:
    expected = {email.id: email.expected_action for email in emails if email.expected_action}
    hits = 0
    total = 0
    per_topic: dict[str, Counter] = {}
    misses = []
    for item in classifications:
        want = expected.get(item.email_id)
        if not want:
            continue
        total += 1
        got = item.action.value
        counter = per_topic.setdefault(item.topic, Counter())
        counter["total"] += 1
        if got == want:
            hits += 1
            counter["hit"] += 1
        else:
            misses.append({"email_id": item.email_id, "want": want, "got": got, "topic": item.topic})
    return {
        "total": total,
        "hits": hits,
        "accuracy": round(hits / total, 3) if total else 0.0,
        "per_topic": {
            topic: {"total": counter["total"], "hit": counter["hit"],
                    "accuracy": round(counter["hit"] / counter["total"], 3)}
            for topic, counter in per_topic.items()
        },
        "misses": misses,
    }


def run(
    settings: Settings,
    profile_path: Path | None = None,
    journal: Journal | None = None,
    emails: list[Email] | None = None,
    source: str | None = None,
) -> RunResult:
    data_dir = settings.data_dir
    profile_path = profile_path or data_dir / "leader_profile.md"
    out_dir = settings.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    profile = load_profile(profile_path)
    emails = emails if emails is not None else load_inbox(data_dir / "inbox.csv")
    org = load_org(data_dir / "org_structure.csv")
    tasks = load_tasks(data_dir / "tasks.csv")
    events = load_calendar(data_dir / "calendar.ics")
    contacts = load_contacts(data_dir / "contacts.csv")
    protocols = parse_all(data_dir / "meetings", data_dir / "protocols")
    write_protocols(protocols, out_dir / "protocols")

    kb = KnowledgeBase.build(profile, org, tasks, events, protocols)

    own_journal = journal is None
    journal = journal or Journal(settings.db_path)
    run_id = journal.start_run(profile.id, source or str(data_dir))
    hints = journal.learning_hints()

    raw = [classify(email, profile, org, tasks, events) for email in emails]
    safety_guarded = sum(
        1 for item in raw if set(item.risks) & SENSITIVE_RISKS and item.action != Action.ASK
    )
    classifications = [
        _enforce_safety(email, item, settings.confidence_threshold)
        for email, item in zip(emails, raw)
    ]
    for item in classifications:
        if not item.assignee_email and item.assignee:
            item.assignee_email = contacts.get(item.assignee.lower(), "")
    drafts = []
    for email, item in zip(emails, classifications):
        payload = item.as_dict()
        payload.update(
            subject=email.subject,
            from_name=email.from_name,
            received=email.received.strftime("%Y-%m-%d %H:%M"),
            expected=email.expected_action,
        )
        journal.save_classification(run_id, payload)
        draft = build_draft(item, email, profile, kb, hints=hints, now=email.received)
        if draft is not None:
            drafts.append(draft)
            journal.save_draft(run_id, draft.as_dict())
    for protocol in protocols:
        journal.save_protocol(run_id, protocol.as_dict())

    accuracy = _accuracy(emails, classifications)
    action_counts = Counter(item.action.value for item in classifications)
    categories = Counter(email.category for email in emails)
    drafts_with_sources = sum(1 for draft in drafts if draft.sources)
    auto_sends = sum(1 for draft in drafts if draft.sent)
    morning = morning_plan(emails[0].received if emails else datetime.now(), emails, classifications, events, tasks, protocols)
    digest = weekly_digest(emails[0].received if emails else datetime.now(), emails, classifications, events, tasks, protocols)
    economics_report = econ.build_report(settings)
    rules_report = rules_audit(profile, emails, org, tasks, events)
    rules_eval = rules_report["email"]["eval"]

    metrics = {
        "profile_name": profile.name,
        "profile_role": profile.role,
        "mode": "live" if source else "dataset",
        "email_source": source or "dataset",
        "emails": len(emails),
        "drafts": len(drafts),
        "decisions": action_counts.get(Action.ASK.value, 0),
        "delegated": action_counts.get(Action.DELEGATE.value, 0),
        "auto": action_counts.get(Action.AUTO.value, 0),
        "digest": action_counts.get(Action.DIGEST.value, 0),
        "ignored": action_counts.get(Action.IGNORE.value, 0),
        "actions": dict(action_counts),
        "categories": dict(categories),
        "accuracy": accuracy,
        "citations_coverage": round(drafts_with_sources / len(drafts), 3) if drafts else 0.0,
        "auto_sends": auto_sends,
        "safety_guarded": safety_guarded,
        "safety_violations": 0,
        "protocols": len(protocols),
        "protocol_tasks": sum(len(protocol.tasks) for protocol in protocols),
        "protocol_open_questions": sum(len(protocol.open_questions) for protocol in protocols),
        "protected_hours_week": digest["protected_hours"],
        "meetings_week": digest["meetings"],
        "economics": economics_report,
        "learning_hints": sorted(hints),
        "rules": {
            "threads": rules_report["email"]["threads_total"],
            "thread_accuracy": rules_eval["thread_accuracy"],
            "email_accuracy": rules_eval["email_accuracy"],
            "sensitive_auto": len(rules_eval["sensitive_auto"]),
            "calendar_changes": len(rules_report["calendar"]["changes"]),
            "focus_hours_week": rules_report["calendar"]["focus_hours_week"],
            "overdue_tasks": rules_report["tasks"]["overdue"],
            "status_mismatches": [item["id"] for item in rules_report["tasks"]["status_mismatches"]],
            "plan_date": rules_report["plan"]["date"],
        },
    }
    journal.finish_run(run_id, metrics)
    journal.log(
        "run",
        {
            "emails": len(emails),
            "drafts": len(drafts),
            "protocols": len(protocols),
            "auto_sends": auto_sends,
            "profile": profile.name,
        },
    )

    report = {
        "run_id": run_id,
        "profile": {"id": profile.id, "name": profile.name, "role": profile.role},
        "metrics": metrics,
        "morning": morning,
        "digest": digest,
        "protocols": [protocol.as_dict() for protocol in protocols],
        "drafts": [draft.as_dict() for draft in drafts],
        "classifications": [item.as_dict() for item in classifications],
        "economics_text": econ.explain(settings, economics_report),
        "rules": rules_report,
    }
    files: dict[str, str] = {}
    report_json = out_dir / "report.json"
    _write_json(report_json, report)
    files["report_json"] = str(report_json)

    report_md = out_dir / "report.md"
    accuracy_line = (
        f"Точность разбора: {accuracy['hits']}/{accuracy['total']} "
        f"({accuracy['accuracy']:.1%})" if accuracy["total"] else "Точность: нет разметки"
    )
    lines = [
        f"# Отчёт AsiStantAI · {profile.name}",
        "",
        f"Писем: {metrics['emails']} · черновиков: {metrics['drafts']} · автоотправок: {metrics['auto_sends']}",
        f"Источник: {metrics['email_source']}",
        accuracy_line,
        f"Решений на руководителя: {metrics['decisions']} · делегировано: {metrics['delegated']} · "
        f"в сводку: {metrics['digest']} · пропущено: {metrics['ignored']} · авто: {metrics['auto']}",
        f"Протоколов: {metrics['protocols']} · задач из протоколов: {metrics['protocol_tasks']} · "
        f"открытых вопросов: {metrics['protocol_open_questions']}",
        f"Ссылки на источники в черновиках: {metrics['citations_coverage']:.0%}",
        "",
        "## Правила из файлов",
        f"Тем: {metrics['rules']['threads']}, точность по темам {metrics['rules']['thread_accuracy']:.0%}; "
        f"чувствительных тем в авто: {metrics['rules']['sensitive_auto']}",
        f"Календарь: автопереносов {metrics['rules']['calendar_changes']}; фокус "
        + ", ".join(f"{week}: {hours:g} ч" for week, hours in metrics["rules"]["focus_hours_week"].items()),
        f"Задачи: просрочены {', '.join(metrics['rules']['overdue_tasks']) or 'нет'}; "
        f"статус расходится: {', '.join(metrics['rules']['status_mismatches']) or 'нет'}",
        f"План на {metrics['rules']['plan_date']} — в report.json, раздел rules",
        "",
        "## Экономика",
        *[f"- {line}" for line in report["economics_text"]],
        "",
        "## Утренний план",
        f"{morning['date']}: встреч {len(morning['events'])}, защищённых {morning['protected_hours']} ч.",
        *[f"- {item}" for item in morning["focus"]],
        "",
        "## Что делать дальше",
        "1. Открыть дашборд и проверить решения в очереди.",
        "2. Утвердить или поправить черновики — правки станут шаблоном темы.",
        "3. Разослать протоколы и поставить задачи.",
        "",
    ]
    report_md.write_text("\n".join(lines), encoding="utf-8")
    files["report_md"] = str(report_md)

    morning_json = out_dir / "morning_plan.json"
    _write_json(morning_json, morning)
    files["morning_json"] = str(morning_json)

    digest_json = out_dir / "weekly_digest.json"
    _write_json(digest_json, digest)
    files["digest_json"] = str(digest_json)
    digest_md = out_dir / "weekly_digest.md"
    digest_md.write_text(digest_markdown(digest), encoding="utf-8")
    files["digest_md"] = str(digest_md)

    drafts_jsonl = out_dir / "drafts.jsonl"
    with drafts_jsonl.open("w", encoding="utf-8") as fh:
        for draft in drafts:
            fh.write(json.dumps(draft.as_dict(), ensure_ascii=False) + "\n")
    files["drafts_jsonl"] = str(drafts_jsonl)

    inbox_csv = out_dir / "inbox_classified.csv"
    with inbox_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "received", "from_name", "subject", "action", "expected", "topic", "confidence"])
        for email, item in zip(emails, classifications):
            writer.writerow([
                email.id,
                email.received.strftime("%Y-%m-%d %H:%M"),
                email.from_name,
                email.subject,
                item.action.value,
                email.expected_action,
                item.topic,
                f"{item.confidence:.2f}",
            ])
    files["inbox_csv"] = str(inbox_csv)
    files["protocols_dir"] = str(out_dir / "protocols")

    if own_journal:
        journal.close()

    return RunResult(
        run_id=run_id,
        profile=profile,
        emails=emails,
        classifications=classifications,
        drafts=drafts,
        protocols=protocols,
        morning=morning,
        digest=digest,
        metrics=metrics,
        economics=economics_report,
        out_dir=out_dir,
        files=files,
    )

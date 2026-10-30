"""aiogram entry point; domain and access control remain in Store."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import shlex
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from .calendar import CalendarError, CalendarProvider, configure_calendar_provider
from .intent import Intent, IntentError, IntentProvider, OpenAIIntentProvider, validate_intent
from .store import Store

PENDING_INTENT_TTL_SECONDS = 15 * 60
MAX_PENDING_INTENTS = 1_000


def format_task(row, timezone_name: str = "UTC") -> str:
    due = f" — {datetime.fromisoformat(row['due_at']).astimezone(ZoneInfo(timezone_name)).strftime('%Y-%m-%d %H:%M %Z')}" if row["due_at"] else ""
    return f"#{row['id']} {row['title']}{due}"


def resolve_local_datetime(value: datetime, timezone_name: str) -> datetime:
    """Attach an IANA timezone only when the local wall time maps to one instant."""
    if value.tzinfo is not None:
        raise ValueError("Expected a timezone-naive local date and time")
    zone = ZoneInfo(timezone_name)
    candidates = [value.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
    valid = [
        candidate for candidate in candidates
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == value
    ]
    if not valid:
        raise ValueError("Local time does not exist because of a timezone clock change")
    if len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset():
        raise ValueError("Local time occurs twice because of a timezone clock change")
    return valid[0]


def briefing_due_date(now: datetime, timezone_name: str, send_hour: int) -> str | None:
    if now.tzinfo is None:
        raise ValueError("Clock must be timezone-aware")
    if not 0 <= send_hour <= 23:
        raise ValueError("Briefing hour must be between 0 and 23")
    local = now.astimezone(ZoneInfo(timezone_name))
    return local.date().isoformat() if local.hour == send_hour else None


def configure_intent_provider(enabled: str, api_key: str, model: str) -> IntentProvider | None:
    """Require explicit configuration opt-in as well as credentials."""
    if enabled.strip().lower() not in {"1", "true", "yes"}:
        return None
    if not api_key.strip() or not model.strip():
        raise ValueError("OPENAI_ENABLED requires OPENAI_API_KEY and OPENAI_MODEL")
    return OpenAIIntentProvider(api_key.strip(), model.strip())


def parse_task_command(text: str) -> tuple[str, str | None, int]:
    """Parse optional leading project/priority switches while keeping plain text tasks simple."""
    try:
        parts=shlex.split(text)
    except ValueError as exc:
        raise ValueError("Проверьте кавычки в команде /add.") from exc
    project=None;priority=0
    while parts and parts[0].startswith("--"):
        option=parts.pop(0)
        if option not in {"--project", "--priority"} or not parts:
            raise ValueError("Формат: /add [--project название] [--priority 0..3] текст задачи")
        value=parts.pop(0)
        if option=="--project":project=value.strip() or None
        else:
            try:priority=int(value)
            except ValueError as exc:raise ValueError("Priority должен быть целым числом 0..3.") from exc
            if not 0<=priority<=3:raise ValueError("Priority должен быть целым числом 0..3.")
    title=" ".join(parts).strip()
    if not title:raise ValueError("Добавьте текст задачи после параметров.")
    return title,project,priority


async def deliver_daily_briefing(bot: Bot, store: Store, user_id: int, now: datetime,
                                 send_hour: int, render) -> bool:
    """Claim once per local date; never retry an uncertain Telegram send automatically."""
    local_date = briefing_due_date(now, store.get_timezone(user_id), send_hour)
    if local_date is None or not store.claim_daily_briefing(user_id, local_date):
        return False
    try:
        text=render(user_id, now)
        if inspect.isawaitable(text):text=await text
        await bot.send_message(user_id, text)
    except (TimeoutError, TelegramAPIError, OSError):
        store.finish_daily_briefing(user_id, local_date, False)
        return False
    store.finish_daily_briefing(user_id, local_date, True)
    return True


async def send_reminder_with_rate_limit_retry(bot, user_id: int, text: str,
                                             max_attempts: int = 3) -> None:
    """Retry only Telegram's explicit 429 rejection; ambiguous network sends are never repeated."""
    for attempt in range(max_attempts):
        try:
            await bot.send_message(user_id,text)
            return
        except TelegramRetryAfter as exc:
            if attempt + 1 >= max_attempts or exc.retry_after > 60:
                raise
            await asyncio.sleep(exc.retry_after)


def create_app(token: str, allowlist: set[int], store: Store, briefing_hour: int = 8,
               intent_provider: IntentProvider | None = None,
               calendar_provider: CalendarProvider | None = None) -> tuple[Bot, Dispatcher]:
    if not 0 <= briefing_hour <= 23:
        raise ValueError("Briefing hour must be between 0 and 23")
    bot, dp = Bot(token=token), Dispatcher()
    pending_intents: dict[str, tuple[int, Intent, float]] = {}

    async def allowed(message: Message) -> bool:
        if message.chat.type != "private":
            await message.answer("Личный inbox работает только в приватном чате с ботом.")
            return False
        if not message.from_user or message.from_user.id not in allowlist:
            await message.answer("Доступ закрыт. Попросите владельца добавить ваш Telegram ID в ALLOWED_USER_IDS.")
            return False
        return True

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        if await allowed(message):
            await message.answer("Личный inbox готов. /help — список команд.")

    @dp.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        if await allowed(message):
            await message.answer("/add [--project название] [--priority 0..3] текст задачи\n/add YYYY-MM-DD HH:MM | задача\n/note [--important] текст #тег\n/note-important ID on|off\n/ai <запрос> — optional LLM preview; изменения только после подтверждения\n/today, /inbox, /done ID, /search слова, /briefing\n/reminders [history], /cancelreminder ID\n/timezone Europe/Moscow\n/quiet 23 8")

    @dp.message(Command("add"))
    async def add(message: Message) -> None:
        if not await allowed(message): return
        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            await message.answer("Использование: /add текст задачи или /add YYYY-MM-DD HH:MM | задача")
            return
        project=None;priority=0;due = None
        if not text.startswith("20") or " | " not in text:
            try:text,project,priority=parse_task_command(text)
            except ValueError as exc:await message.answer(str(exc));return
        if " | " in text:
            when, text = text.split(" | ",1)
            try:
                local = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M")
                due = resolve_local_datetime(local, store.get_timezone(message.from_user.id))
            except ValueError:
                await message.answer("Дата должна существовать в вашей timezone и однозначно задавать время; пропущенные или повторяющиеся часы при переводе времени не принимаются.")
                return
        try:
            task_id = store.add_task(message.from_user.id, text, due,project,priority)
        except ValueError as exc:
            await message.answer(str(exc)); return
        await message.answer(f"Добавлено: #{task_id} {text}" + (f" — {due.isoformat()}" if due else ""))

    @dp.message(Command("note"))
    async def note(message: Message) -> None:
        if not await allowed(message): return
        text = (message.text or "").partition(" ")[2].strip()
        important=text.startswith("--important ")
        if important:text=text.removeprefix("--important ").strip()
        tags = [word[1:] for word in text.split() if word.startswith("#")]
        try:
            note_id = store.add_note(message.from_user.id, text, tags,important)
        except ValueError as exc:
            await message.answer(str(exc)); return
        await message.answer(f"Заметка сохранена: #{note_id}"+(" (важная)" if important else ""))

    @dp.message(Command("note-important"))
    async def note_important(message: Message) -> None:
        if not await allowed(message):return
        parts=(message.text or "").split()
        if len(parts)!=3 or parts[2].lower() not in {"on","off"}:
            await message.answer("Формат: /note-important ID on|off");return
        try:note_id=int(parts[1])
        except ValueError:await message.answer("ID заметки должен быть числом.");return
        changed=store.set_note_important(message.from_user.id,note_id,parts[2].lower()=="on")
        await message.answer("Важность заметки обновлена." if changed else "Заметка не найдена.")

    async def show_tasks(message: Message, title: str) -> None:
        if not await allowed(message): return
        uid = message.from_user.id
        rows = store.tasks(uid)
        await message.answer(title + ("\n" + "\n".join(format_task(r, store.get_timezone(uid)) for r in rows[:30]) if rows else "\nПока пусто."))

    @dp.message(Command("inbox"))
    async def inbox(message: Message) -> None: await show_tasks(message, "Открытые задачи:")

    @dp.message(Command("today"))
    async def today(message: Message) -> None:
        if not await allowed(message): return
        uid = message.from_user.id
        tz = ZoneInfo(store.get_timezone(uid)); now = datetime.now(tz)
        rows = store.tasks(uid)
        chosen = []
        for row in rows:
            due = datetime.fromisoformat(row["due_at"]) if row["due_at"] else None
            if due and due.astimezone(tz).date() <= now.date(): chosen.append(row)
        await message.answer("На сегодня и просрочено:" + ("\n" + "\n".join(format_task(r, store.get_timezone(uid)) for r in chosen) if chosen else "\nЗадач нет."))

    @dp.message(Command("done"))
    async def done(message: Message) -> None:
        if not await allowed(message): return
        arg = (message.text or "").partition(" ")[2].strip()
        try: task_id = int(arg)
        except ValueError:
            await message.answer("Укажите ID: /done 12"); return
        await message.answer("Задача завершена." if store.complete(message.from_user.id,task_id) else "Открытая задача с таким ID не найдена.")

    @dp.message(Command("search"))
    async def search(message: Message) -> None:
        if not await allowed(message): return
        query = (message.text or "").partition(" ")[2].strip()
        if not query: await message.answer("Укажите текст: /search договор"); return
        tasks, notes = store.search(message.from_user.id,query)
        parts = [*(format_task(r, store.get_timezone(message.from_user.id)) for r in tasks[:10]), *(f"Заметка #{r['id']}: {r['body']} [{r['tags']}]" for r in notes[:10])]
        await message.answer("\n".join(parts) if parts else "Совпадений нет.")

    @dp.message(Command("timezone"))
    async def timezone_cmd(message: Message) -> None:
        if not await allowed(message): return
        value = (message.text or "").partition(" ")[2].strip()
        try: store.set_timezone(message.from_user.id,value)
        except ValueError as exc: await message.answer(str(exc)); return
        await message.answer(f"Timezone установлена: {value}")

    @dp.message(Command("quiet"))
    async def quiet(message: Message) -> None:
        if not await allowed(message): return
        parts=(message.text or "").split()
        if len(parts)!=3:
            await message.answer("Формат: /quiet 23 8 — тихие часы с 23:00 до 08:00.");return
        try: store.set_quiet_hours(message.from_user.id,int(parts[1]),int(parts[2]))
        except ValueError as exc: await message.answer(str(exc));return
        await message.answer(f"Тихие часы установлены: {parts[1]}:00–{parts[2]}:00 по вашей timezone.")

    @dp.message(Command("reminders"))
    async def reminders(message: Message) -> None:
        if not await allowed(message):
            return
        uid = message.from_user.id
        argument = (message.text or "").partition(" ")[2].strip().lower()
        if argument:
            if argument != "history":
                await message.answer("Формат: /reminders или /reminders history")
                return
            rows = store.reminder_history(uid, limit=10)
            if not rows:
                await message.answer("История доставки напоминаний пока пуста.")
                return
            tz = ZoneInfo(store.get_timezone(uid))
            status_labels = {"sent": "доставлено", "failed": "ошибка", "unknown": "результат неизвестен"}
            lines = []
            for row in rows:
                attempted = datetime.fromisoformat(row["attempted_at"]).astimezone(tz)
                detail = f" — {row['detail']}" if row["detail"] else ""
                lines.append(
                    f"{attempted.strftime('%Y-%m-%d %H:%M %Z')} · "
                    f"#{row['reminder_id']} задача #{row['task_id']}: {row['title']} · "
                    f"{status_labels.get(row['outcome'], row['outcome'])}{detail}"
                )
            await message.answer("Последние попытки доставки:\n" + "\n".join(lines))
            return
        rows = store.reminders(uid)
        tz = ZoneInfo(store.get_timezone(uid))
        if not rows:
            await message.answer("Активных напоминаний нет.")
            return
        lines = [
            f"#{row['id']} задача #{row['task_id']}: {row['title']} — "
            f"{datetime.fromisoformat(row['due_at']).astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')}"
            for row in rows[:30]
        ]
        await message.answer("Напоминания (отмена: /cancelreminder ID):\n" + "\n".join(lines))

    @dp.message(Command("cancelreminder"))
    async def cancel_reminder(message: Message) -> None:
        if not await allowed(message):
            return
        arg = (message.text or "").partition(" ")[2].strip()
        try:
            reminder_id = int(arg)
        except ValueError:
            await message.answer("Укажите ID напоминания: /cancelreminder 12")
            return
        changed = store.cancel_reminder(message.from_user.id, reminder_id)
        await message.answer(
            "Напоминание отменено." if changed else "Активное напоминание с таким ID не найдено."
        )

    def render_briefing(uid: int, now: datetime) -> str:
        tz=ZoneInfo(store.get_timezone(uid)); now=now.astimezone(tz)
        rows=store.tasks(uid); overdue=[]; today_rows=[]; upcoming=[]
        for row in rows:
            if not row["due_at"]: continue
            due=datetime.fromisoformat(row["due_at"]).astimezone(tz)
            if due.date() < now.date(): overdue.append(row)
            elif due.date() == now.date(): today_rows.append(row)
            elif (due-now).days < 8: upcoming.append(row)
        notes=store.search(uid,"")[1]
        important_notes=[n for n in notes if n["important"]][:5]
        recent_notes=[n for n in notes if not n["important"]][:5]
        sections=[("Просрочено",overdue),("Сегодня",today_rows),("Ближайшие дедлайны",upcoming)]
        out=[]
        for label,items in sections:
            out.append(label+":"+("\n"+"\n".join(format_task(x, store.get_timezone(uid)) for x in items) if items else " нет"))
        if important_notes:out.append("Важные заметки:\n"+"\n".join(f"#{n['id']} {n['body']}" for n in important_notes))
        if recent_notes: out.append("Последние заметки:\n"+"\n".join(f"#{n['id']} {n['body']}" for n in recent_notes))
        return "\n\n".join(out)

    async def render_briefing_with_calendar(uid: int, now: datetime) -> str:
        result=render_briefing(uid,now)
        if calendar_provider is None:return result
        timezone_name=store.get_timezone(uid);tz=ZoneInfo(timezone_name);local_now=now.astimezone(tz)
        start=local_now.replace(hour=0,minute=0,second=0,microsecond=0);end=start+timedelta(days=1)
        try:events=await asyncio.to_thread(calendar_provider.events_between,start,end,timezone_name)
        except CalendarError:
            return result+"\n\nКалендарь временно недоступен; сводка показана без событий календаря."
        calendar_lines=[f"{event.starts_at} — {event.title}" for event in events[:30]]
        return result+"\n\nКалендарь:"+("\n"+"\n".join(calendar_lines) if calendar_lines else " событий нет")

    @dp.message(Command("briefing"))
    async def briefing(message: Message) -> None:
        if not await allowed(message): return
        await message.answer(await render_briefing_with_calendar(message.from_user.id, datetime.now(UTC)))

    @dp.message(Command("ai"))
    async def ai_intent(message: Message) -> None:
        if not await allowed(message): return
        text=(message.text or "").partition(" ")[2].strip()
        if intent_provider is None:
            await message.answer("LLM отключён. Настройте OPENAI_API_KEY и OPENAI_MODEL либо используйте /add и /note.")
            return
        if not text:
            await message.answer("Формат: /ai напомни завтра позвонить клиенту. LLM предложит preview, запись появится только после подтверждения.")
            return
        try:
            timezone_name=store.get_timezone(message.from_user.id)
            local_now=datetime.now(ZoneInfo(timezone_name))
            raw=await asyncio.to_thread(intent_provider.interpret,text,timezone_name=timezone_name,now=local_now)
            intent=validate_intent(raw)
        except IntentError as exc:
            await message.answer(str(exc));return
        except Exception:  # noqa: BLE001 - fail closed for injected providers; never mutate on failure
            await message.answer("Не удалось обработать запрос. Используйте /add или /note; данные не изменены.");return
        if intent.action=="clarify":
            await message.answer(intent.question)
            return
        uid=message.from_user.id
        key=f"{uid}:{message.message_id}"
        created_at=time.monotonic()
        for expired_key, (_, _, created) in list(pending_intents.items()):
            if created_at-created > PENDING_INTENT_TTL_SECONDS:
                pending_intents.pop(expired_key,None)
        if len(pending_intents) >= MAX_PENDING_INTENTS:
            await message.answer("Слишком много ожидающих подтверждений. Подтвердите или отмените старое preview.")
            return
        pending_intents[key]=(uid,intent,created_at)
        if intent.action=="add_task":
            local_due=intent.due_at.astimezone(ZoneInfo(store.get_timezone(uid))).strftime("%Y-%m-%d %H:%M %Z") if intent.due_at else "без срока"
            preview=f"Задача: {intent.content}\nСрок: {local_due}"
        else:
            tags=("\nТеги: "+", ".join(intent.tags)) if intent.tags else ""
            preview=f"Заметка: {intent.content}{tags}"
        keyboard=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить",callback_data=f"ai-confirm:{key}"),InlineKeyboardButton(text="Отмена",callback_data=f"ai-cancel:{key}")]])
        await message.answer("Проверьте распознанное действие:\n"+preview+"\n\nНичего не сохранено до подтверждения.",reply_markup=keyboard)

    @dp.callback_query(F.data.startswith("ai-confirm:"))
    async def confirm_ai_intent(call: CallbackQuery) -> None:
        if (not call.from_user or call.from_user.id not in allowlist
                or not call.message or call.message.chat.type != "private"):
            await call.answer("Доступ закрыт",show_alert=True);return
        key=call.data.removeprefix("ai-confirm:");pending=pending_intents.get(key)
        if pending is None:
            await call.answer("Preview истёк или уже обработан",show_alert=True);return
        if pending[0]!=call.from_user.id:
            await call.answer("Это preview другого пользователя",show_alert=True);return
        _,intent,created_at=pending
        if time.monotonic()-created_at > PENDING_INTENT_TTL_SECONDS:
            pending_intents.pop(key,None)
            await call.answer("Preview истёк; запросите новое распознавание",show_alert=True);return
        pending_intents.pop(key,None)
        if intent.action=="add_task":
            task_id=store.add_task(call.from_user.id,intent.content,intent.due_at)
            response=f"Задача #{task_id} сохранена."
        else:
            note_id=store.add_note(call.from_user.id,intent.content,list(intent.tags))
            response=f"Заметка #{note_id} сохранена."
        if call.message: await call.message.edit_text(response)
        await call.answer()

    @dp.callback_query(F.data.startswith("ai-cancel:"))
    async def cancel_ai_intent(call: CallbackQuery) -> None:
        if (not call.from_user or call.from_user.id not in allowlist
                or not call.message or call.message.chat.type != "private"):
            await call.answer("Доступ закрыт",show_alert=True);return
        key=call.data.removeprefix("ai-cancel:");pending=pending_intents.get(key)
        if pending and pending[0]!=call.from_user.id:
            await call.answer("Это preview другого пользователя",show_alert=True);return
        pending_intents.pop(key,None)
        if call.message: await call.message.edit_text("Предложенное действие отменено; данные не изменены.")
        await call.answer()

    @dp.message(F.text & ~F.text.startswith("/"))
    async def add_plain_text(message: Message) -> None:
        """Treat an ordinary message as a task, keeping slash commands explicit."""
        if not await allowed(message):
            return
        text = (message.text or "").strip()
        try:
            task_id = store.add_task(message.from_user.id, text)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"Добавлено: #{task_id} {text}")

    async def reminder_loop() -> None:
        while True:
            now=datetime.now(UTC)
            store.recover_uncertain_reminders(now)
            for row in store.due_reminders(now):
                tz=ZoneInfo(row["timezone"] or "UTC"); local=now.astimezone(tz); hour=local.hour
                start=row["quiet_start"] if row["quiet_start"] is not None else int(os.getenv("QUIET_HOURS_START","23"))
                end=row["quiet_end"] if row["quiet_end"] is not None else int(os.getenv("QUIET_HOURS_END","8"))
                quiet=(start <= hour < end) if start > end else (hour >= start or hour < end)
                if quiet: continue
                if not store.claim_reminder(row["id"],row["user_id"]): continue
                try:
                    await send_reminder_with_rate_limit_retry(bot,row["user_id"],f"⏰ Напоминание: {row['title']} (задача #{row['task_id']})")
                    store.finish_reminder(row["id"],True)
                except (TimeoutError, TelegramAPIError, OSError) as exc:  # no retry after an uncertain delivery result
                    store.finish_reminder(row["id"],False,type(exc).__name__)
            for user_id in allowlist:
                await deliver_daily_briefing(bot,store,user_id,now,briefing_hour,render_briefing_with_calendar)
            await asyncio.sleep(30)

    async def start_reminder_loop() -> None:
        asyncio.create_task(reminder_loop())

    dp.startup.register(start_reminder_loop)
    return bot,dp


def main() -> None:
    parser=argparse.ArgumentParser(description="Personal AI Chief of Staff Telegram bot")
    parser.parse_args()
    load_dotenv()
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    allowed={int(v) for v in os.getenv("ALLOWED_USER_IDS","").split(",") if v.strip().isdigit()}
    if not token or not allowed:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and at least one ALLOWED_USER_IDS in .env")
    store=Store(os.getenv("DATABASE_PATH","chief_of_staff.sqlite3"))
    for user_id in allowed:
        store.ensure_user_settings(user_id,os.getenv("USER_TIMEZONE","UTC"),int(os.getenv("QUIET_HOURS_START","23")),int(os.getenv("QUIET_HOURS_END","8")))
    try:
        briefing_hour=int(os.getenv("BRIEFING_HOUR","8"))
        if not 0<=briefing_hour<=23: raise ValueError
    except ValueError as exc:
        raise SystemExit("BRIEFING_HOUR must be an integer from 0 to 23") from exc
    try:
        provider=configure_intent_provider(os.getenv("OPENAI_ENABLED", "false"),os.getenv("OPENAI_API_KEY", ""),os.getenv("OPENAI_MODEL", ""))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        calendar=configure_calendar_provider(os.getenv("CALENDAR_ENABLED", "false"),os.getenv("GOOGLE_CALENDAR_ACCESS_TOKEN", ""),os.getenv("GOOGLE_CALENDAR_ID", "primary"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    bot,dp=create_app(token,allowed,store,briefing_hour,provider,calendar)
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__": main()

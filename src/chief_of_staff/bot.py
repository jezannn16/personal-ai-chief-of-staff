"""aiogram entry point; domain and access control remain in Store."""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

from .store import Store


def format_task(row, timezone_name: str = "UTC") -> str:
    due = f" — {datetime.fromisoformat(row['due_at']).astimezone(ZoneInfo(timezone_name)).strftime('%Y-%m-%d %H:%M %Z')}" if row["due_at"] else ""
    return f"#{row['id']} {row['title']}{due}"


def create_app(token: str, allowlist: set[int], store: Store) -> tuple[Bot, Dispatcher]:
    bot, dp = Bot(token=token), Dispatcher()

    async def allowed(message: Message) -> bool:
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
            await message.answer("/add текст задачи | /add YYYY-MM-DD HH:MM | задача\n/note текст #тег\n/today, /inbox, /done ID, /search слова, /briefing\n/reminders, /cancelreminder ID\n/timezone Europe/Moscow\n/quiet 23 8")

    @dp.message(Command("add"))
    async def add(message: Message) -> None:
        if not await allowed(message): return
        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            await message.answer("Использование: /add текст задачи или /add YYYY-MM-DD HH:MM | задача")
            return
        due = None
        if " | " in text:
            when, text = text.split(" | ",1)
            try:
                local = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(store.get_timezone(message.from_user.id)))
                due = local
            except ValueError:
                await message.answer("Дата должна быть в формате YYYY-MM-DD HH:MM и существовать в вашей timezone.")
                return
        try:
            task_id = store.add_task(message.from_user.id, text, due)
        except ValueError as exc:
            await message.answer(str(exc)); return
        await message.answer(f"Добавлено: #{task_id} {text}" + (f" — {due.isoformat()}" if due else ""))

    @dp.message(Command("note"))
    async def note(message: Message) -> None:
        if not await allowed(message): return
        text = (message.text or "").partition(" ")[2].strip()
        tags = [word[1:] for word in text.split() if word.startswith("#")]
        try:
            note_id = store.add_note(message.from_user.id, text, tags)
        except ValueError as exc:
            await message.answer(str(exc)); return
        await message.answer(f"Заметка сохранена: #{note_id}")

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

    @dp.message(Command("briefing"))
    async def briefing(message: Message) -> None:
        if not await allowed(message): return
        uid=message.from_user.id; tz=ZoneInfo(store.get_timezone(uid)); now=datetime.now(tz)
        rows=store.tasks(uid); overdue=[]; today_rows=[]; upcoming=[]
        for row in rows:
            if not row["due_at"]: continue
            due=datetime.fromisoformat(row["due_at"]).astimezone(tz)
            if due.date() < now.date(): overdue.append(row)
            elif due.date() == now.date(): today_rows.append(row)
            elif (due-now).days < 8: upcoming.append(row)
        notes=store.search(uid,"")[1][:5]
        sections=[("Просрочено",overdue),("Сегодня",today_rows),("Ближайшие дедлайны",upcoming)]
        out=[]
        for label,items in sections:
            out.append(label+":"+("\n"+"\n".join(format_task(x, store.get_timezone(uid)) for x in items) if items else " нет"))
        if notes: out.append("Заметки:\n"+"\n".join(f"#{n['id']} {n['body']}" for n in notes))
        await message.answer("\n\n".join(out))

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
                    for attempt in range(3):
                        try:
                            await bot.send_message(row["user_id"],f"⏰ Напоминание: {row['title']} (задача #{row['task_id']})")
                            break
                        except TelegramRetryAfter as exc:
                            if attempt==2 or exc.retry_after>60: raise
                            await asyncio.sleep(exc.retry_after)
                    store.finish_reminder(row["id"],True)
                except (TimeoutError, TelegramAPIError, OSError) as exc:  # no retry after an uncertain delivery result
                    store.finish_reminder(row["id"],False,type(exc).__name__)
            await asyncio.sleep(30)

    async def start_reminder_loop() -> None:
        asyncio.create_task(reminder_loop())

    dp.startup.register(start_reminder_loop)
    return bot,dp


def main() -> None:
    load_dotenv()
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    allowed={int(v) for v in os.getenv("ALLOWED_USER_IDS","").split(",") if v.strip().isdigit()}
    if not token or not allowed:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and at least one ALLOWED_USER_IDS in .env")
    store=Store(os.getenv("DATABASE_PATH","chief_of_staff.sqlite3"))
    for user_id in allowed:
        store.ensure_user_settings(user_id,os.getenv("USER_TIMEZONE","UTC"),int(os.getenv("QUIET_HOURS_START","23")),int(os.getenv("QUIET_HOURS_END","8")))
    bot,dp=create_app(token,allowed,store)
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__": main()

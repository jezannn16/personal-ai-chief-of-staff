import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, Update, User

from chief_of_staff.bot import briefing_due_date, create_app
from chief_of_staff.store import Store


def test_startup_schedules_reminder_loop(tmp_path):
    async def run_startup():
        bot, dispatcher = create_app(
            "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            {1},
            Store(tmp_path / "startup.sqlite3"),
        )
        reminder_tasks = []
        try:
            await dispatcher.emit_startup(bot=bot)
            reminder_tasks = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_coro().__name__ == "reminder_loop"
            ]
            assert len(reminder_tasks) == 1
        finally:
            for task in reminder_tasks:
                task.cancel()
            await bot.session.close()
            await asyncio.gather(*reminder_tasks, return_exceptions=True)

    asyncio.run(run_startup())


def test_plain_text_update_creates_persistent_task(tmp_path):
    async def run_update():
        store = Store(tmp_path / "plain-text.sqlite3")
        bot, dispatcher = create_app(
            "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", {42}, store
        )
        update = Update(
            update_id=1,
            message=Message(
                message_id=7,
                date=datetime.now(UTC),
                chat=Chat(id=42, type="private"),
                from_user=User(id=42, is_bot=False, first_name="Test"),
                text="  Тестовая задача Codex  ",
            ),
        )
        try:
            with patch("aiogram.client.bot.Bot.__call__", new_callable=AsyncMock) as call_bot:
                await dispatcher.feed_update(bot, update)

            call_bot.assert_awaited_once()
            assert call_bot.await_args.args[0].text == "Добавлено: #1 Тестовая задача Codex"
            rows = Store(tmp_path / "plain-text.sqlite3").tasks(42)
            assert len(rows) == 1
            assert rows[0]["title"] == "Тестовая задача Codex"
            assert rows[0]["status"] == "open"
        finally:
            await bot.session.close()

    asyncio.run(run_update())


def test_scheduled_briefing_uses_user_local_date_and_configured_hour():
    from datetime import datetime

    now = datetime(2026, 10, 30, 5, 0, tzinfo=UTC)
    assert briefing_due_date(now, "Europe/Moscow", 8) == "2026-10-30"
    assert briefing_due_date(now, "Europe/Moscow", 9) is None

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, Update, User

from chief_of_staff.bot import briefing_due_date, create_app, deliver_daily_briefing
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


def test_daily_briefing_send_is_claimed_once_with_fake_bot(tmp_path):
    from datetime import timedelta

    class FakeBot:
        def __init__(self):
            self.sent=[]

        async def send_message(self,user_id,text):
            self.sent.append((user_id,text))

    store=Store(tmp_path/"briefing.sqlite3");store.set_timezone(51,"Europe/Moscow")
    bot=FakeBot();now=datetime(2026,10,30,5,tzinfo=UTC)
    render=lambda uid,clock:f"briefing for {uid} at {clock.isoformat()}"
    assert asyncio.run(deliver_daily_briefing(bot,store,51,now,8,render)) is True
    assert asyncio.run(deliver_daily_briefing(bot,store,51,now+timedelta(minutes=1),8,render)) is False
    assert bot.sent==[(51,render(51,now))]
    with store._connect() as db:
        row=db.execute("SELECT state FROM daily_briefing_delivery WHERE user_id=51 AND local_date='2026-10-30'").fetchone()
    assert row["state"]=="sent"


def test_uncertain_daily_briefing_send_is_not_repeated(tmp_path):
    from chief_of_staff.store import Store

    class UncertainBot:
        calls=0

        async def send_message(self,user_id,text):
            self.calls+=1
            raise TimeoutError("synthetic timeout")

    store=Store(tmp_path/"failed-briefing.sqlite3");store.set_timezone(52,"UTC")
    bot=UncertainBot();now=datetime(2026,10,30,8,tzinfo=UTC)
    render=lambda _uid,_clock:"synthetic briefing"
    assert asyncio.run(deliver_daily_briefing(bot,store,52,now,8,render)) is False
    assert asyncio.run(deliver_daily_briefing(bot,store,52,now,8,render)) is False
    assert bot.calls==1
    with store._connect() as db:
        row=db.execute("SELECT state FROM daily_briefing_delivery WHERE user_id=52 AND local_date='2026-10-30'").fetchone()
    assert row["state"]=="failed"

# Demo

1. Start the bot with a private test account in `ALLOWED_USER_IDS`.
2. Set timezone with `/timezone Europe/Moscow`, add a task and a note, then verify `/inbox` and `/search`.
3. Add a reminder with `/add 2030-05-01 15:30 | Demo reminder`; check the user-facing confirmation and the UTC value in SQLite.
4. Restart the process and confirm the task remains. Use `/done <id>` and repeat it; the second update reports no open task.
5. Test `/today` and `/briefing`. No test contacts Telegram; the scheduler calls Telegram only when the configured bot is running.

Reminder delivery claims are persisted before contacting Telegram, so concurrent polling cannot claim the same row twice.

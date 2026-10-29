# Personal AI Chief of Staff

Локальный Telegram inbox для задач, заметок и напоминаний. Команды работают детерминированно и не требуют LLM.

## Реализовано

- `/start`, `/help`, `/add`, `/note`, `/today`, `/inbox`, `/done`, `/search`, `/timezone`, `/quiet`, `/briefing`.
- SQLite хранит задачи, заметки, настройки timezone, очередь напоминаний и журнал доставки.
- Списки, поиск и завершение всегда ограничены Telegram user ID. Бот запускается только при заданном allowlist.
- Напоминания переживают перезапуск. Атомарный claim предотвращает повторную отправку; при сбое доставки запись закрывается и видна в журнале.
- Briefing показывает просроченные и сегодняшние задачи, дедлайны на ближайшую неделю и последние заметки.

## Запуск

Нужен Python 3.11+. Скопируйте `.env.example` в `.env`, создайте бота через BotFather, заполните `TELEGRAM_BOT_TOKEN` и свой numeric Telegram ID в `ALLOWED_USER_IDS`.

```sh
uv venv --python 3.12
uv pip install -e '.[dev]'
cp .env.example .env
uv run chief-of-staff
```

`/add 2030-05-01 15:30 | Позвонить` задаёт локальное время пользователя. Даты в SQLite хранятся как ISO-8601 UTC и выводятся в локальной timezone. Установите её командой `/timezone Europe/Moscow`; quiet hours задаются, например, `/quiet 23 8`.

## Проверки

```sh
uv run pytest
uv run ruff check src tests
```

## Ограничения

Telegram-доставка использует at-most-once claim. Явный Telegram 429 повторяется до двух раз с Retry-After; timeout/неопределённая сетевая ошибка не повторяется, чтобы избежать двойного сообщения, а запись отмечается failed в `reminder_delivery_log`. Quiet hours откладывают напоминание до следующего прохода, но нет UI просмотра/отмены журнала. LLM и календарный OAuth адаптер не включены. Удаление данных выполняется удалением локальной SQLite базы.

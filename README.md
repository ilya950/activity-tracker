# Activity Tracker — Telegram-бот учёта активности

Бот ведёт учёт смен модераторов в Google-таблице и сохраняет скриншоты
входа/выхода в Google Drive (через веб-приложение Google Apps Script).

## Запуск

    python main.py

Секретов в репозитории нет — всё берётся из переменных окружения:

| Переменная | Обязательно | Что это |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | да | токен от @BotFather |
| `SPREADSHEET_ID` | да | ID Google-таблицы |
| `SHEET_NAME` | нет | лист для записей, по умолчанию `Лист1` |
| `GOOGLE_CREDENTIALS_JSON` | да | содержимое credentials.json сервисного аккаунта одной строкой |
| `SHEETS_PROXY_URL` | да | адрес веб-приложения Apps Script |
| `SHEETS_PROXY_SECRET` | да | секрет веб-приложения |

Локально вместо переменных можно положить файл `.env` в корень проекта.

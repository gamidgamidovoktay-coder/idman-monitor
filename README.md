# Idman Monitor

Первый рабочий агрегатор для мониторинга азербайджанского спорта.

## Что делает

- Мониторит 41 источник.
- Ищет только новости, связанные с азербайджанским спортом.
- Проверяет главную страницу и первую страницу спортивных/релевантных разделов.
- Не отправляет дубли по смыслу.
- Если новость впервые обнаружена, берёт её даже при старом времени публикации.
- Финально досканирует за 2–3 минуты до отправки.
- Сортирует сверху самые свежие.
- Пишет заголовок, краткое описание на языке оригинала, источник, дополнительные источники и ссылку.
- Если новостей нет, письмо не отправляет.
- Если сайт не открылся, указывает его в конце письма.
- Исключает букмекерскую/ставочную тематику по словам: букмекер, букмекеры, ставки, stavka, bukmeker.

## Исключённые источники

- stadium.az
- idman-az.com
- goal.az
- chess.az
- sportmedia.az
- sportlife.az

## Как запускать локально

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python monitor.py
```

## Render

На Render лучше запускать как Cron Job каждые 30 минут.

Cron command:

```bash
python monitor.py
```

Schedule:

```text
*/30 * * * *
```

Environment Variables:

- SMTP_USER
- SMTP_APP_PASSWORD
- EMAIL_FROM
- EMAIL_TO
- CONFIG_PATH = sources.yaml
- DB_PATH = idman_monitor.sqlite3

Важно: для Gmail нужен App Password, а не обычный пароль.

# Idman Monitor v5

Исправляет реальные проблемы v4:
- постоянная память через DATABASE_URL (PostgreSQL);
- повторы между письмами;
- очередь pending_items для новостей сверх лимита 50;
- жёсткий фильтр: только последние 60 минут;
- новости без даты публикации не берутся;
- Final scan отсутствует.

Важно: для Render Cron нужен DATABASE_URL, иначе SQLite может не сохраняться между запусками.


## v5.1

Исправление PostgreSQL-драйвера:
- `psycopg2-binary` заменён на `pg8000`, потому что `psycopg2-binary` падал в Render на Python 3.14.
- Остальная логика v5 сохранена: постоянная память, очередь pending, окно 60 минут, без Final scan.


## v5.2

Fix:
- Removed invalid `pg8000.connect(DATABASE_URL, sslmode='require')`.
- Now parses DATABASE_URL and connects with `pg8000.dbapi.connect(..., ssl_context=True)`.


## v5.3

Fix:
- Corrected remaining variable reference from `pg8000` to `pgdb`.
- Fixes `NameError: name 'pg8000' is not defined`.


## v5.4

Fix:
- Исправлена ошибка v5.3: теперь `DATABASE_URL` корректно разбирается через `urlparse`.
- Подключение к PostgreSQL идёт через `pg8000.dbapi` и SSL context.
- Проверено: нет `psycopg2`, нет `sslmode`, нет старого `pg8000.connect(DATABASE_URL, ...)`.


## v5.5 final check

- Same PostgreSQL driver fix as v5.4 (`pg8000.dbapi` + SSL context).
- Fixes queue behavior: when 50 selected items are deduplicated into fewer email blocks, all 50 selected items are marked as processed, so hidden duplicates do not come again.
- Items above the 50 limit remain in `pending_items` for the next email.
- Keeps strict freshness: published date required, and date must be within 60 minutes.


## v5.6

Fixes Render PostgreSQL SSL self-signed certificate error by using pg8000 with ssl._create_unverified_context().


## v5.7

Fix:
- Added both DB APIs: `q()/rows()` and `execute()/fetchall()`.
- Fixes `AttributeError: 'DB' object has no attribute 'q'`.
- Keeps v5.6 PostgreSQL SSL fix.

# Deploying the Classroom Booking System to Render

## What changed from your original files

1. **Folder structure fixed** — templates moved into `templates/`, CSS into
   `static/css/`, matching what Flask expects.
2. **Bug fixed** — `reset__password.html` (double underscore) was renamed to
   `reset_password.html` to match the `render_template()` call in `app.py`.
   Without this fix, "Forgot Password" would have crashed with a
   `TemplateNotFound` error.
3. **Database migrated from SQLite to PostgreSQL** — `app.py`'s `get_db()`
   and every query now go through a small wrapper (`DBWrapper`) that talks
   to Postgres via `psycopg2` instead of the `sqlite3` module. This was
   necessary because Render's free web services don't keep a persistent
   disk — your `classroom.db` file would get wiped on every redeploy.
   - `?` placeholders are auto-converted to `%s` inside the wrapper, so
     none of your query strings needed manual editing.
   - `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY` in the
     three `CREATE TABLE` statements (Postgres syntax).
   - `except sqlite3.IntegrityError` → `except psycopg2.IntegrityError`
     in the 3 places that catch duplicate-entry errors.
4. **`requirements.txt` added**, including `gunicorn` (production server)
   and `psycopg2-binary` (Postgres driver).

## What I could NOT verify

I don't have a Postgres server available in this environment to actually
run your app end-to-end against, so this conversion was done by careful
code review and syntax-checking (`app.py` compiles cleanly), not a live
test. **Test it locally against a real Postgres database before you rely
on it**, or test directly on Render's free tier where mistakes are cheap
to fix. If something breaks, paste me the error and I'll fix it fast.

## Missing files

Your templates reference two images that weren't in your upload:
- `static/images/favicon.png`
- `static/images/unilesa_logo.png`

The app will still run without them — you'll just see a broken image icon
where the logo should appear. Add them to `static/images/` whenever you
have them.

## Deployment steps

1. **Push this folder to a GitHub repo** (as-is — the structure is already
   correct).

2. **Create a free PostgreSQL database on Render**
   - Dashboard → New → PostgreSQL → choose free tier → Create.
   - Once ready, Render gives your web service access to it automatically
     via the `DATABASE_URL` env var if you create the web service in the
     same Render account (or copy the "Internal Database URL" manually).

3. **Create a Web Service on Render**
   - New → Web Service → connect your GitHub repo.
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`

4. **Set environment variables** (Web Service → Environment tab) — see
   `.env.example` for the full list: `SECRET_KEY`, `DATABASE_URL` (if not
   auto-linked), `MAIL_USERNAME`, `MAIL_PASSWORD`, `ADMIN_PASSWORD`.

5. **Deploy.** On first run, `create_table()` will automatically create
   the `users`, `bookings`, and `classrooms` tables and seed your default
   lecture halls + the admin account — check your Render logs for the
   printed admin password confirmation.

6. **Test the full flow**: register, login, book a hall, edit/delete a
   booking, and specifically the "Forgot Password" link (since that's the
   file we just fixed).

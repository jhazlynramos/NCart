# NCart Marketplace System

NCart is a marketplace web application for a college community. It supports
student account verification and buying and selling items through a MySQL-backed
web interface.

## Features

- Student registration, account verification, and admin approval workflows
- Marketplace listings, profiles, favorites, and a shopping cart
- Orders, payment receipts, messages, and notifications
- Reports, complaints, and administrative review tools
- Optional Google sign-in and email delivery through SMTP

## Technologies

- Python
- Flask components and Python's built-in `ThreadingHTTPServer`
- MySQL and `mysql-connector-python`
- HTML, CSS, and JavaScript

The current local entry point is `app.py`; it starts a `ThreadingHTTPServer` at
`http://127.0.0.1:8000`. Flask is also used by the route modules and is listed
as a dependency.

## Requirements

- Python 3
- MySQL Server
- Git (to clone the repository)

## Local setup

From the project root, create and activate a virtual environment:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Create a local environment file from the placeholder template:

```powershell
Copy-Item .env.example .env
```

Edit `.env` with your local MySQL settings. Set `NCART_SESSION_SECRET` to a
private random value (for example, generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`). Do not commit
`.env`. The `.env.example` values are placeholders and must be replaced locally.

Configure `NCART_GOOGLE_CLIENT_ID`, `NCART_GOOGLE_CLIENT_SECRET`, and
`NCART_GOOGLE_REDIRECT_URI` only if using Google sign-in. Configure the
`NCART_SMTP_*` variables only if the application should send email.

## MySQL setup

1. Start MySQL Server.
2. Set `NCART_DB_HOST`, `NCART_DB_PORT`, `NCART_DB_USER`,
   `NCART_DB_PASSWORD`, and `NCART_DB_NAME` in `.env`.
3. Create the database and schema using the safe schema script:

   ```powershell
   mysql -u root -p < schema.sql
   ```

   The script creates the `ncart` database and its tables if they do not
   already exist. Alternatively, the application initializes the schema when it
   starts; the configured MySQL user must have permission to create the database
   and tables.

`schema.sql` contains schema definitions, not application user records.
Personal student verification images, payment receipts, and runtime uploads
should remain local and are excluded from Git.

## Run locally

With the virtual environment active and `.env` configured, start the application:

```powershell
python app.py
```

Open <http://127.0.0.1:8000>. Stop the server with `Ctrl+C`.

## Tests

Run the available tests with:

```powershell
python -m unittest discover -s tests
```

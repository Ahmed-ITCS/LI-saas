# LI_Bot — Django Edition

Multi-user LinkedIn auto-comment bot with a dark terminal dashboard.

## Features

- **Auth**: Sign up / login (Django built-in, open registration)
- **Multi-profile**: Each user can add multiple LinkedIn accounts
- **Encrypted credentials**: AES-128 via Fernet — passwords never stored in plaintext
- **Post age filter**: Optional per-profile min/max age window (e.g. 30–360 min). Leave either bound empty to disable that side of the filter.
- **LLM**: Gemini with automatic key rotation on 429, or Mock fallback
- **Bot control**: Start/Stop each profile independently
- **Parallel mode**: Each profile runs as its own subprocess
- **Live logs**: SSE streaming log tail per profile
- **Comment history**: Every comment stored with post snippet + age

---

## Setup

### 1. Install dependencies

```bash
cd libot
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
DJANGO_SECRET_KEY=<run: python -c "import secrets; print(secrets.token_hex(32))">
FERNET_KEY=<run: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
DEBUG=true
```

### 3. Run migrations

```bash
python manage.py migrate
```

### 4. (Optional) Create a superuser for Django admin

```bash
python manage.py createsuperuser
```

### 5. Start the server

```bash
python manage.py runserver 0.0.0.0:8000
```

Open http://localhost:8000 — sign up, add a LinkedIn profile, and start the bot.

---

## Project Structure

```
libot/
├── manage.py
├── requirements.txt
├── .env.example
│
├── libot/               # Django project
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
│
├── accounts/            # Auth + LinkedIn profile model
│   ├── models.py        # LinkedInProfile, CommentLog
│   ├── forms.py         # SignUpForm, LinkedInProfileForm (handles encryption)
│   ├── crypto.py        # Fernet encrypt/decrypt helpers
│   ├── views.py         # signup, login, logout
│   └── urls.py
│
├── dashboard/           # Web UI
│   ├── views.py         # index, profile CRUD, bot start/stop, SSE, API
│   └── urls.py
│
├── bot/
│   └── runner.py        # Standalone subprocess: Playwright + Gemini + age filter
│
├── templates/
│   ├── base.html
│   ├── accounts/
│   │   ├── login.html
│   │   └── signup.html
│   └── dashboard/
│       ├── index.html          # Profile grid + global stats
│       ├── profile_detail.html # Live logs + comment history
│       └── profile_form.html   # Add/Edit profile
│
├── logs/        # Auto-created: profile_<id>.log per profile
└── states/      # Auto-created: profile_<id>.json (Playwright session)
```

---

## Per-profile settings (all configurable in the UI)

| Setting | Description |
|---|---|
| Label | Friendly name for this LinkedIn account |
| LinkedIn email/password | Stored encrypted via Fernet |
| LLM provider | Gemini (with key rotation) or Mock |
| Gemini API keys | One per line, rotated on 429 |
| Min post age | Skip posts newer than N minutes (leave empty for no lower bound) |
| Max post age | Skip posts older than N minutes (leave empty for no upper bound) |
| Max comments/round | How many comments per feed scan (default 6) |
| Persona prompt | Injected before every LLM call |
| Run parallel | Own subprocess vs sequential |

---

## How post age filtering works

LinkedIn shows relative timestamps in the feed DOM like `"45m"`, `"1h"`, `"2h"`, `"3d"`.
The bot parses these strings and converts to minutes before applying the min/max filter.
Posts whose age can't be parsed are processed without an age check (so non-English locales
and unusual DOMs aren't accidentally filtered out).

Min/Max are both optional — leaving a field empty means "no bound on that side".
Leaving both empty disables the post-age filter entirely.

---

## Production notes

- Set `DEBUG=false` and `ALLOWED_HOSTS` properly in `.env`
- Use gunicorn: `gunicorn libot.wsgi -b 0.0.0.0:8000 --workers 2 --threads 4`
- Put nginx in front for SSL
- The SSE endpoint (`/stream/logs/<pk>/`) needs a long-lived connection — ensure your proxy doesn't time it out (`proxy_read_timeout 3600`)
# LI-saas

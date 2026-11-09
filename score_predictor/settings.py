import os
import dj_database_url
from pathlib import Path
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(env_path):
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(BASE_DIR / ".env")

# ── Security ──────────────────────────────────────────────────────────────────

SECRET_KEY = os.environ.get(
    "SECRET_KEY",
    "django-insecure-*36l33oaifpbnaty_+^qc)_0v%^=r+)*yz5dde!me37j%uv(wt",
)

DEBUG = os.environ.get("DEBUG", "True") == "True"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]

# HTTPS is enabled only when the deployment explicitly configures it. The
# Oracle VM currently serves HTTP through Nginx; forcing HTTPS without a TLS
# listener makes the entire site unreachable.
USE_HTTPS = os.environ.get("USE_HTTPS", "True").strip().lower() in {
    "1", "true", "yes", "on"
}

# Respect explicitly configured origins (including the active HTTP IP origin).
configured_csrf_origins = os.environ.get("CSRF_TRUSTED_ORIGINS", "").strip()
if configured_csrf_origins:
    CSRF_TRUSTED_ORIGINS = [
        origin.strip()
        for origin in configured_csrf_origins.split(",")
        if origin.strip()
    ]
else:
    scheme = "https" if USE_HTTPS else "http"
    CSRF_TRUSTED_ORIGINS = [
        f"{scheme}://{host}"
        for host in ALLOWED_HOSTS
        if host not in {"127.0.0.1", "localhost"}
    ] + ["http://127.0.0.1:8000", "http://localhost:8000"]

CORS_ALLOW_ALL_ORIGINS = DEBUG

# Empty entries can occur when the environment variable is unset, blank, or
# contains a trailing comma. django-cors-headers rejects "" as an origin.
# Strip whitespace and discard empty values before Django's system checks.
CORS_ALLOWED_ORIGINS = (
    [
        origin.strip()
        for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if not DEBUG
    else []
)

# ── Apps ──────────────────────────────────────────────────────────────────────

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "predict",
    "django_celery_beat",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
]

# ── Middleware ────────────────────────────────────────────────────────────────

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",   # static files in production
    "django.middleware.cache.UpdateCacheMiddleware",  # page cache (must be first after security)
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.cache.FetchFromCacheMiddleware",  # page cache (must be last)
]

CACHE_MIDDLEWARE_SECONDS = 300      # cache pages for 5 minutes
CACHE_MIDDLEWARE_KEY_PREFIX = "ixtips"

ROOT_URLCONF = "score_predictor.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "score_predictor.wsgi.application"

# ── Database ──────────────────────────────────────────────────────────────────
# Uses PostgreSQL on Railway (DATABASE_URL env var), falls back to SQLite locally.

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# ── Password validation ───────────────────────────────────────────────────────

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── Internationalisation ──────────────────────────────────────────────────────

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

# ── Static files ──────────────────────────────────────────────────────────────

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── External APIs ─────────────────────────────────────────────────────────────

FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
FOOTBALL_DATA_BASE_URL = os.environ.get(
    "FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4"
)

# ── Cache ─────────────────────────────────────────────────────────────────────
# Redis/Celery removed: the scheduled jobs now run via supercronic + the
# `run_task` management command (see crontab), and caching is Postgres-backed.
# This eliminates the Upstash dependency whose monthly command limit was being
# exhausted by Celery's 24/7 broker polling.

CACHES = {
    # Postgres-backed cache — odds, standings, metadata, small data.
    # Table created via `manage.py createcachetable` in the release command.
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "ix_tips_cache",
        # High limit so page-cache keys don't evict training-data entries
        "OPTIONS": {"MAX_ENTRIES": 10000, "CULL_FREQUENCY": 4},
    },
    # File-based — large ML model bundles (sklearn models are big)
    "model_cache": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": "/tmp/ix_tips_model_cache",
        "TIMEOUT": 86400,  # 24 hours
    },
}

# ── Django REST Framework ─────────────────────────────────────────────────────

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ),
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# ── Auth redirects ────────────────────────────────────────────────────────────

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/admin-dashboard/"
LOGOUT_REDIRECT_URL = "/login/"

# ── HTTPS security (production only) ─────────────────────────────────────────

if not DEBUG:
    SECURE_SSL_REDIRECT = USE_HTTPS
    SESSION_COOKIE_SECURE = USE_HTTPS
    CSRF_COOKIE_SECURE = USE_HTTPS
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

    # Only advertise HSTS when HTTPS is actually enabled and served.
    if USE_HTTPS:
        SECURE_HSTS_SECONDS = 31536000
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        SECURE_HSTS_PRELOAD = True

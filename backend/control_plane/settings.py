"""Django settings for control_plane project."""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, "dev-secret-key-change-me"),
    ALLOWED_HOSTS=(list, []),
)

def _load_env_file() -> None:
    env_file = Path(env.str("DJANGO_ENV_FILE", default=str(BASE_DIR / ".env")))
    if env_file.exists():
        environ.Env.read_env(env_file, overwrite=False)


_load_env_file()

DEBUG = env("DEBUG")
_default_allowed_hosts = ["localhost", "127.0.0.1", "testserver", "0.0.0.0"]
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS") or _default_allowed_hosts
SECRET_KEY = env("SECRET_KEY")

INSTALLED_APPS = [
    "daphne",  # Must be first for ASGI support
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",  # WebSocket support
    "rest_framework",
    "drf_spectacular",
    "configuration.apps.ConfigurationConfig",
    "acquisition.apps.AcquisitionConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "control_plane.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "control_plane.wsgi.application"
ASGI_APPLICATION = "control_plane.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env.str("DJANGO_DB_NAME", default=str(BASE_DIR / "db.sqlite3")),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

SPECTACULAR_SETTINGS = {
    "TITLE": "工业数据采集控制平台 API",
    "DESCRIPTION": "阶段 M1 基础接口文档",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
# IMPORTANT: Always use Celery worker for async tasks, never run in Django process
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = env.bool("CELERY_TASK_EAGER_PROPAGATES", default=True)
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=600)

# InfluxDB Settings
INFLUXDB_HOST = env.str("INFLUXDB_HOST", default="localhost")
INFLUXDB_PORT = env.int("INFLUXDB_PORT", default=8086)
INFLUXDB_TOKEN = env.str("INFLUXDB_TOKEN", default="")
INFLUXDB_ORG = env.str("INFLUXDB_ORG", default="default")
INFLUXDB_BUCKET = env.str("INFLUXDB_BUCKET", default="default")

# Kafka Settings (Optional)
KAFKA_ENABLED = env.bool("KAFKA_ENABLED", default=False)
KAFKA_BOOTSTRAP_SERVERS = env.str("KAFKA_BOOTSTRAP_SERVERS", default="localhost:9092")
KAFKA_TOPIC = env.str("KAFKA_TOPIC", default="acquisition_data")

# Logging Configuration
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(BASE_DIR / "logs" / "application.log"),
            "maxBytes": 10485760,
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": "INFO",
        },
        "acquisition": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "storage": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
}


# ========================================
# Django Channels Configuration
# ========================================

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [(
                env.str("REDIS_HOST", default="127.0.0.1"),
                env.int("REDIS_PORT", default=6379),
            )],
            "capacity": 1500,  # Maximum number of messages in a channel
            "expiry": 10,  # Message expiry in seconds
        },
    },
}

# ========================================
# Acquisition Service Configuration
# ========================================

# Batch size for data collection before writing to storage
ACQUISITION_BATCH_SIZE = env.int("ACQUISITION_BATCH_SIZE", default=50)

# Maximum time to wait before flushing batch (seconds)
ACQUISITION_BATCH_TIMEOUT = env.float("ACQUISITION_BATCH_TIMEOUT", default=5.0)

# Connection timeout - mark device as timeout after this period without successful read (seconds)
ACQUISITION_CONNECTION_TIMEOUT = env.float("ACQUISITION_CONNECTION_TIMEOUT", default=30.0)

# Maximum number of consecutive reconnection attempts before giving up
ACQUISITION_MAX_RECONNECT_ATTEMPTS = env.int("ACQUISITION_MAX_RECONNECT_ATTEMPTS", default=3)

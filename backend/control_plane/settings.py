"""Django settings for control_plane project."""
from pathlib import Path

import environ
from celery.schedules import crontab

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
_default_allowed_hosts = ["localhost", "127.0.0.1", "testserver", "0.0.0.0", "django", "host.docker.internal"]
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
    "fleet.apps.FleetConfig",
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
        "ATOMIC_REQUESTS": False,
        # SQLite serialises writers with a database-level lock. The acquisition
        # pipeline runs many worker/sink threads that all write, so the default
        # 5 s busy-timeout surfaces as "database is locked" errors under load.
        # `timeout` makes a blocked writer wait up to 30 s for the lock; the
        # ORM hands connections across threads (workers, Channels) so
        # `check_same_thread` must be disabled.
        "OPTIONS": {
            "timeout": 30,
            "check_same_thread": False,
        },
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
    # H10: global pagination so list endpoints never dump an unbounded result
    # set. We use limit/offset pagination so the contract is consistent with
    # the custom data-points action and the frontend API client: clients page
    # via ?limit=N&offset=M and responses are {count, next, previous, results}.
    # default_limit falls back to PAGE_SIZE; StandardLimitOffsetPagination caps
    # max_limit. Custom @action endpoints that build their own Response are
    # unaffected.
    "DEFAULT_PAGINATION_CLASS": "control_plane.pagination.StandardLimitOffsetPagination",
    "PAGE_SIZE": env.int("DRF_PAGE_SIZE", default=50),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "工业数据采集控制平台 API",
    "DESCRIPTION": "阶段 M1 基础接口文档",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default=f"redis://{env('REDIS_HOST', default='localhost')}:{env('REDIS_PORT', default=6379)}/0")
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
# IMPORTANT: Always use Celery worker for async tasks, never run in Django process
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = env.bool("CELERY_TASK_EAGER_PROPAGATES", default=True)
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=600)

# M12: retention window for uploaded Excel import jobs/files, purged by the
# scheduled ``cleanup_import_jobs`` task below.
IMPORT_JOB_RETENTION_DAYS = env.int("IMPORT_JOB_RETENTION_DAYS", default=30)

# Celery beat schedule — requires running `celery -A control_plane beat`.
CELERY_BEAT_SCHEDULE = {
    "cleanup-import-jobs-daily": {
        "task": "configuration.tasks.cleanup_import_jobs",
        # Daily at 03:30 — off-peak for an industrial acquisition gateway.
        "schedule": crontab(hour=3, minute=30),
    },
}

# InfluxDB Settings
INFLUXDB_HOST = env.str("INFLUXDB_HOST", default="localhost")
INFLUXDB_PORT = env.int("INFLUXDB_PORT", default=8086)
INFLUXDB_TOKEN = env.str("INFLUXDB_TOKEN", default="")
INFLUXDB_ORG = env.str("INFLUXDB_ORG", default="default")
INFLUXDB_BUCKET = env.str("INFLUXDB_BUCKET", default="default")
# InfluxDB durable spill queue path. Empty = storage layer's own default
# (next to the backend package). On a read-only edge mount the edge-agent
# overrides this via the INFLUXDB_SPILL_DB_PATH env so the queue lands on a
# writable volume; an unwritable path degrades gracefully (spill disabled).
INFLUXDB_SPILL_DB_PATH = env.str("INFLUXDB_SPILL_DB_PATH", default="")

# WebSocketSink broadcast cadence (seconds). Doubles as the M3 distributed
# ``sample_batch`` aggregation window — the edge-agent sets this from
# EDGE_UPLINK_SAMPLE_WINDOW. 1 Hz for a monolith deployment.
WS_BROADCAST_INTERVAL_S = env.float("WS_BROADCAST_INTERVAL_S", default=1.0)

# M3 distributed uplink — center side. When enabled the fleet WS bridge
# also mirrors inbound edge ``sample_batch`` frames into InfluxDB under the
# ``edge_sample`` measurement (intended for a short-retention bucket;
# default 7 days, applied as the bucket's retention policy). The aggregation
# cache table (``EdgeSample``) is always written regardless of this flag.
CENTER_EDGE_SAMPLE_TO_INFLUX = env.bool("CENTER_EDGE_SAMPLE_TO_INFLUX", default=True)
CENTER_EDGE_SAMPLE_RETENTION_DAYS = env.int("CENTER_EDGE_SAMPLE_RETENTION_DAYS", default=7)

# Kafka Settings (Optional)
KAFKA_ENABLED = env.bool("KAFKA_ENABLED", default=False)
KAFKA_BOOTSTRAP_SERVERS = env.str("KAFKA_BOOTSTRAP_SERVERS", default="localhost:9092")
KAFKA_TOPIC = env.str("KAFKA_TOPIC", default="acquisition_data")

# Logging Configuration
#
# M6: the ``acquisition`` logger defaults to INFO (was DEBUG — every read
# cycle spammed the file). Override per-environment with ACQUISITION_LOG_LEVEL.
# The file handler is an AsyncRotatingFileHandler: records are queued and a
# background thread does the disk write + rollover, so logging never blocks an
# acquisition cycle.
#
# M2 (XIU-59): the log directory is now env-configurable via DJANGO_LOG_DIR.
# The edge-agent imports ``control_plane.settings`` to run the acquisition
# pipeline, but mounts the center code read-only (``backend:/opt/backend:ro``)
# — writing into ``BASE_DIR/logs`` there raises OSError and aborts
# ``django.setup()``. We therefore (a) let the edge point DJANGO_LOG_DIR at a
# writable path, and (b) probe writability and gracefully fall back to
# console-only logging if the directory cannot be written, so importing the
# settings never crashes on a read-only filesystem.
LOG_DIR = Path(env.str("DJANGO_LOG_DIR", default=str(BASE_DIR / "logs")))

_file_logging_ok = True
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _probe = LOG_DIR / ".write_probe"
    _probe.touch()
    _probe.unlink()
except OSError:
    # Read-only mount (edge-agent) or otherwise unwritable — keep console
    # logging only. The acquisition pipeline still logs to stdout/journald.
    _file_logging_ok = False

ACQUISITION_LOG_LEVEL = env.str("ACQUISITION_LOG_LEVEL", default="INFO").upper()

_VERBOSE_LOG_FORMAT = "{levelname} {asctime} {module} {process:d} {thread:d} {message}"

# Handler set shared by every logger below — file is included only when the
# log directory is writable.
_LOG_HANDLERS = ["console", "file"] if _file_logging_ok else ["console"]

_LOG_HANDLER_DEFS = {
    "console": {
        "level": "INFO",
        "class": "logging.StreamHandler",
        "formatter": "simple",
    },
}
if _file_logging_ok:
    _LOG_HANDLER_DEFS["file"] = {
        "level": "INFO",
        # Async queue handler — owns its own RotatingFileHandler + listener
        # thread. The target handler carries the verbose format (fmt/style),
        # so no "formatter" key is set on the queue handler itself.
        "class": "control_plane.logging_utils.AsyncRotatingFileHandler",
        "filename": str(LOG_DIR / "application.log"),
        "maxBytes": 10485760,
        "backupCount": 5,
        "fmt": _VERBOSE_LOG_FORMAT,
        "style": "{",
    }

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": _VERBOSE_LOG_FORMAT,
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": _LOG_HANDLER_DEFS,
    "loggers": {
        "django": {
            "handlers": _LOG_HANDLERS,
            "level": "INFO",
        },
        "acquisition": {
            "handlers": _LOG_HANDLERS,
            "level": ACQUISITION_LOG_LEVEL,
            "propagate": False,
        },
        "storage": {
            "handlers": _LOG_HANDLERS,
            "level": "INFO",
            "propagate": False,
        },
        # modbus_tk emits a benign ERROR each cycle when the upstream Modbus
        # reply lingers in the socket buffer; the library auto-reconnects and
        # the next read succeeds. Suppress to CRITICAL to keep logs readable.
        "modbus_tk": {
            "handlers": _LOG_HANDLERS,
            "level": "CRITICAL",
            "propagate": False,
        },
        "modbus_tcp": {
            "handlers": _LOG_HANDLERS,
            "level": "CRITICAL",
            "propagate": False,
        },
    },
    "root": {
        "handlers": _LOG_HANDLERS,
        "level": "INFO",
    },
}


# ========================================
# Django Channels Configuration
# ========================================
#
# WebSocket fan-out assumes a SINGLE acquisition process. The pipeline's
# WebSocketSink broadcasts each session's readings to the channel-layer
# groups; if the AcquisitionService runs in more than one process (e.g.
# Celery concurrency > 1, or the pipeline started in both web + worker),
# every copy broadcasts and connected clients receive duplicates.
# Deploy the acquisition worker with concurrency 1 (or converge broadcasts
# into a single dedicated task). The WebSocketSink additionally caps each
# frame's payload size (see sinks.py:_WS_MAX_READINGS_PER_MSG) so a large
# session cannot exceed the per-message limits of the layer below.
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

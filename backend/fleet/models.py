"""Models for the center-side edge fleet registry.

Each `EdgeNode` represents one edge gateway (one Linux box / 工控机) that runs
the `edge-agent` Python service. The center never stores the activation token
in plaintext; only its salted SHA-256 hash is kept so a token leak from the
DB does not let an attacker impersonate an edge.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Tuple

from django.db import models
from django.utils import timezone


class EdgeStatus(models.TextChoices):
    PENDING = "pending", "pending"
    ONLINE = "online", "online"
    OFFLINE = "offline", "offline"


class EdgeNode(models.Model):
    """One physical edge gateway registered with the center."""

    name = models.CharField(max_length=128, unique=True)
    token_hash = models.CharField(max_length=128, db_index=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    version = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=EdgeStatus.choices,
        default=EdgeStatus.PENDING,
    )
    labels = models.JSONField(default=dict, blank=True)
    # M3: highest uplink ``monotonic_seq`` (lifecycle / sample_batch) the
    # center has accepted from this edge. Drives duplicate / gap / backfill
    # detection — see ``docs/distributed/protocol.md`` § Uplink sequencing.
    # M5: this is now durable across the edge's WS sessions — register no
    # longer resets it (the edge's seq counter is persistent too), so the
    # center can tell a reconnecting edge exactly which frames to backfill.
    last_uplink_seq = models.PositiveBigIntegerField(default=0)
    # M5 (v0.5): depth of the edge's durable uplink outbox, mirrored from
    # the ``buffer`` field on each heartbeat. Non-zero means the edge is
    # sitting on un-shipped frames (offline, or mid-backfill).
    buffer_backlog = models.PositiveBigIntegerField(default=0)
    # M5: last time the edge backfilled replayed frames after a reconnect.
    last_backfill_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.name} ({self.status})"

    # ---- token helpers -----------------------------------------------------

    @staticmethod
    def hash_token(token: str) -> str:
        """Stable, salt-less SHA-256 hex digest of an activation token.

        We deliberately keep this deterministic (no per-row salt) so the
        WebSocket consumer can look an edge up by hashing the presented
        token once. Tokens are 256-bit random strings; brute-forcing a
        SHA-256 of a 256-bit random secret is not a realistic threat.
        """
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def generate_token(cls) -> str:
        """Generate a fresh edge activation token (URL-safe, ~43 chars)."""
        return secrets.token_urlsafe(32)

    @classmethod
    def issue(cls, name: str, *, labels: dict | None = None) -> Tuple["EdgeNode", str]:
        """Create a new EdgeNode and return (node, plaintext_token).

        The plaintext token is shown to the operator exactly once at
        registration time; it is never persisted server-side.
        """
        token = cls.generate_token()
        node = cls.objects.create(
            name=name,
            token_hash=cls.hash_token(token),
            labels=labels or {},
            status=EdgeStatus.PENDING,
        )
        return node, token

    def verify_token(self, token: str) -> bool:
        return hmac.compare_digest(self.token_hash, self.hash_token(token))

    # ---- lifecycle ---------------------------------------------------------

    def mark_online(self, *, version: str | None = None) -> None:
        self.last_seen = timezone.now()
        self.status = EdgeStatus.ONLINE
        # M5: register does NOT reset ``last_uplink_seq``. The edge's seq
        # counter is now persistent (it lives in the edge's durable SQLite
        # outbox and survives a process restart), so the high-water mark
        # stays valid across WS sessions — that is exactly what lets the
        # center tell a reconnecting edge which frames to backfill. The M3
        # short-session-restart concern that motivated the reset no longer
        # applies: the edge resumes its counter from N+1, never from 1.
        update_fields = ["last_seen", "status", "updated_at"]
        if version is not None and version != self.version:
            self.version = version
            update_fields.append("version")
        self.save(update_fields=update_fields)

    def touch_heartbeat(self) -> None:
        self.last_seen = timezone.now()
        self.status = EdgeStatus.ONLINE
        self.save(update_fields=["last_seen", "status", "updated_at"])

    def is_stale(self, *, now=None) -> bool:
        """Whether the edge is currently considered offline.

        XIU-112: presence is now driven solely by the broker's retained
        ``edge/<id>/lwt`` topic — the edge publishes ``online`` on connect
        and the broker's last-will publishes ``offline`` on any ungraceful
        disconnect (see :mod:`fleet.presence`). ``status`` therefore IS the
        single source of truth; we no longer decay an online edge after a
        ``last_seen`` timeout, because an idle edge (no task dispatched)
        legitimately sends no uplink yet stays connected via MQTT keepalive.

        ``now`` is accepted for backward-compatible call signatures but is
        unused — the verdict has no time dependency anymore.
        """
        return self.status not in (EdgeStatus.ONLINE, EdgeStatus.PENDING)


class AssignmentDesiredState(models.TextChoices):
    """What the center wants this (edge, task) pairing to be doing.

    The edge reconciles its local task runners against the latest
    ``desired_state`` whenever a new ``apply_config`` lands; it does not
    take instructions from the center any other way.
    """

    RUNNING = "running", "running"
    STOPPED = "stopped", "stopped"


class EdgeAssignment(models.Model):
    """One row per (edge, task) pairing the center has dispatched.

    ``config_version`` is the per-edge monotonically increasing version
    counter; whenever an assignment row is created, updated, or removed
    the assignment-sync code bumps the edge's overall version and stamps
    the row. ``last_applied_version`` is set when the edge's
    ``config_applied`` arrives so we can show drift in the UI.
    """

    edge = models.ForeignKey(
        EdgeNode,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    # AcqTask lives in `configuration` — use a soft string FK to avoid a
    # circular import at module load (fleet must not depend on configuration).
    task = models.ForeignKey(
        "configuration.AcqTask",
        on_delete=models.CASCADE,
        related_name="edge_assignments",
    )
    desired_state = models.CharField(
        max_length=16,
        choices=AssignmentDesiredState.choices,
        default=AssignmentDesiredState.RUNNING,
    )
    config_version = models.PositiveIntegerField(default=0)
    last_applied_version = models.PositiveIntegerField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Each edge owns a task uniquely (M2 single-owner model). To allow
        # multi-edge fan-out in a later milestone we widen the relation;
        # the unique_together keeps things honest until then.
        unique_together = ("edge", "task")
        ordering = ("edge", "task")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.edge.name}:{self.task_id}@v{self.config_version}"


class EdgeTaskStatus(models.Model):
    """Latest task lifecycle state reported by an edge.

    One row per (edge, task). Updated in place from inbound
    ``task_state`` frames. Cheaper to query than the full ``AcqSession``
    timeline when the UI only needs "what is the current state".
    """

    edge = models.ForeignKey(
        EdgeNode,
        on_delete=models.CASCADE,
        related_name="task_statuses",
    )
    task = models.ForeignKey(
        "configuration.AcqTask",
        on_delete=models.CASCADE,
        related_name="edge_statuses",
    )
    state = models.CharField(max_length=16)
    error = models.TextField(blank=True, default="")
    last_reported_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("edge", "task")
        ordering = ("edge", "task")
        indexes = [
            models.Index(fields=["task", "-last_reported_at"], name="edge_task_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.edge.name}:{self.task_id}={self.state}"


def next_config_version(edge: "EdgeNode") -> int:
    """Compute the next per-edge config_version for an ``apply_config``.

    We piggy-back on the per-row ``config_version`` on ``EdgeAssignment``
    so the counter survives a service restart without an extra table.
    A simple ``MAX(config_version)+1`` is enough — assignment sync runs
    serialized under a row-level lock on the parent edge in the view.
    """
    current_max = (
        EdgeAssignment.objects.filter(edge=edge)
        .order_by("-config_version")
        .values_list("config_version", flat=True)
        .first()
    )
    return int(current_max or 0) + 1


# ---------------------------------------------------------------------------
# M3 — uplink: lifecycle events + aggregated sample cache
# ---------------------------------------------------------------------------


class EdgeLifecycleEvent(models.Model):
    """Append-only timeline of lifecycle events reported by an edge.

    One row per inbound ``lifecycle`` frame, plus center-synthesised
    ``session.offline`` rows written when the WS disconnects. The *latest*
    per-(edge, task) state still lives in :class:`EdgeTaskStatus` for cheap
    "current state" lookups; this table is the audit history behind it.
    """

    edge = models.ForeignKey(
        EdgeNode,
        on_delete=models.CASCADE,
        related_name="lifecycle_events",
    )
    # Null for session-scoped events (session.online / session.offline).
    task = models.ForeignKey(
        "configuration.AcqTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="edge_lifecycle_events",
    )
    event = models.CharField(max_length=32)
    # Uplink seq of the originating frame; 0 for center-synthesised rows.
    monotonic_seq = models.PositiveBigIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    # Edge wall-clock from the frame's ``ts`` (may drift); null if absent.
    edge_ts = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-received_at", "-id")
        indexes = [
            models.Index(fields=["edge", "-received_at"], name="edge_lifecycle_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.edge.name}:{self.event}@{self.monotonic_seq}"


class EdgeSample(models.Model):
    """Center-side aggregation cache: latest sample per (edge, task, point).

    Updated in place from inbound ``sample_batch`` frames — this is the
    汇聚缓存 the operator UI reads for a live value without round-tripping to
    InfluxDB. The optional InfluxDB mirror (short-retention bucket) is
    written separately by the consumer; this table is always kept.
    """

    edge = models.ForeignKey(
        EdgeNode,
        on_delete=models.CASCADE,
        related_name="samples",
    )
    task = models.ForeignKey(
        "configuration.AcqTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="edge_samples",
    )
    point_code = models.CharField(max_length=128)
    # Reading value — number, bool or string; JSONField carries any of them.
    value = models.JSONField(null=True, blank=True)
    quality = models.CharField(max_length=16, default="good")
    # Timestamp of the underlying reading (from the sample's ``timestamp``).
    sample_ts = models.DateTimeField(null=True, blank=True)
    # Uplink seq of the sample_batch frame this value arrived in.
    monotonic_seq = models.PositiveBigIntegerField(default=0)
    window_end = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("edge", "task", "point_code")
        ordering = ("edge", "task", "point_code")
        indexes = [
            models.Index(fields=["edge", "task"], name="edge_sample_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.edge.name}:{self.point_code}={self.value}"


# Outcome of comparing an inbound uplink seq against the stored high-water
# mark. See ``docs/distributed/protocol.md`` § Uplink sequencing.
UPLINK_SEQ_ADVANCED = "advanced"   # first frame of a stream, or seq == prev + 1
UPLINK_SEQ_GAP = "gap"             # seq > prev + 1 — frames lost (M5 backfill)
UPLINK_SEQ_DUPLICATE = "duplicate" # 0 < seq <= prev — replay; apply idempotently


def classify_uplink_seq(prev: int, incoming: int) -> str:
    """Classify an inbound uplink ``monotonic_seq`` against the prior value.

    Pure function (no DB) so it is trivially unit-testable. The caller
    persists ``EdgeNode.last_uplink_seq`` for every outcome except
    ``duplicate`` (which must not move the high-water mark backward).

    ``prev == 0`` means "no uplink frame ever accepted from this edge" — a
    brand-new edge. Its first frame is the start of the stream, not a gap.

    M5: ``last_uplink_seq`` is NOT reset on register anymore. The edge's
    seq counter is persistent (durable outbox) and the center reports its
    high-water mark in the register ack, so a reconnecting edge resumes at
    ``prev + 1`` and backfills any gap proactively. A ``gap`` verdict here
    therefore means the edge skipped frames it could not buffer (outbox
    cap exceeded) — rare, and still recorded so the stream stays monotonic.
    """
    prev = int(prev or 0)
    incoming = int(incoming or 0)
    if prev == 0:
        return UPLINK_SEQ_ADVANCED
    if incoming <= prev:
        return UPLINK_SEQ_DUPLICATE
    if incoming == prev + 1:
        return UPLINK_SEQ_ADVANCED
    return UPLINK_SEQ_GAP

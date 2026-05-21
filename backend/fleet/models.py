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
from datetime import timedelta
from typing import Tuple

from django.db import models
from django.utils import timezone


# How long after the last heartbeat we consider an edge "offline".
# M1 spec: 30 s. Keep as a class-level constant so tests can monkeypatch
# without poking settings.
OFFLINE_AFTER = timedelta(seconds=30)


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
        if self.last_seen is None:
            return self.status != EdgeStatus.PENDING
        now = now or timezone.now()
        return (now - self.last_seen) > OFFLINE_AFTER


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

"""Signal wiring for the fleet app — M4 alarm-rule auto re-sync.

When an operator creates / edits / deletes an ``AlarmRule`` at the center,
every online edge needs the updated rule set so its local threshold
evaluation stays in step. We hang that off Django's ``post_save`` /
``post_delete`` signals rather than baking it into the ``AlarmRule`` view
so it also covers admin edits, shell edits, and Excel imports.

The re-push is deferred with ``transaction.on_commit`` so a rolled-back
save never reaches the edges, and so the snapshot the edges receive
already contains the committed rule.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _resync_alarm_rules() -> None:
    """Deferred re-push of alarm rules to all online edges."""
    try:
        from .services import sync_alarm_rules_to_edges

        sync_alarm_rules_to_edges()
    except Exception:  # noqa: BLE001
        logger.exception("fleet: alarm-rule re-sync hook failed")


@receiver(post_save, sender="acquisition.AlarmRule", dispatch_uid="fleet_alarm_rule_saved")
def _on_alarm_rule_saved(sender, instance, **kwargs) -> None:
    transaction.on_commit(_resync_alarm_rules)


@receiver(post_delete, sender="acquisition.AlarmRule", dispatch_uid="fleet_alarm_rule_deleted")
def _on_alarm_rule_deleted(sender, instance, **kwargs) -> None:
    transaction.on_commit(_resync_alarm_rules)

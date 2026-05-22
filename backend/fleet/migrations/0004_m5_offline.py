"""M5 — offline degradation + backfill: edge durable-buffer observability.

Adds two diagnostic columns to ``EdgeNode`` so the center / UI can show an
edge that is offline or mid-backfill:

* ``buffer_backlog`` — depth of the edge's durable uplink outbox, mirrored
  from the v0.5 ``heartbeat.buffer`` field.
* ``last_backfill_at`` — last time the edge replayed backfilled frames
  after a reconnect (stamped when an uplink frame carries ``backfill``).

No data migration: both columns default to empty for existing rows.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0003_m3_uplink'),
    ]

    operations = [
        migrations.AddField(
            model_name='edgenode',
            name='buffer_backlog',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='edgenode',
            name='last_backfill_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

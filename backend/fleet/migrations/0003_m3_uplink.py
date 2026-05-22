"""M3 — uplink: per-edge sequence counter, lifecycle event log, sample cache."""
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('configuration', '0010_acqtask_edge'),
        ('fleet', '0002_edgetaskstatus_edgeassignment'),
    ]

    operations = [
        migrations.AddField(
            model_name='edgenode',
            name='last_uplink_seq',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.CreateModel(
            name='EdgeLifecycleEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event', models.CharField(max_length=32)),
                ('monotonic_seq', models.PositiveBigIntegerField(default=0)),
                ('error', models.TextField(blank=True, default='')),
                ('edge_ts', models.DateTimeField(blank=True, null=True)),
                ('received_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('edge', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lifecycle_events', to='fleet.edgenode')),
                ('task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='edge_lifecycle_events', to='configuration.acqtask')),
            ],
            options={
                'ordering': ('-received_at', '-id'),
            },
        ),
        migrations.CreateModel(
            name='EdgeSample',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('point_code', models.CharField(max_length=128)),
                ('value', models.JSONField(blank=True, null=True)),
                ('quality', models.CharField(default='good', max_length=16)),
                ('sample_ts', models.DateTimeField(blank=True, null=True)),
                ('monotonic_seq', models.PositiveBigIntegerField(default=0)),
                ('window_end', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('edge', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='samples', to='fleet.edgenode')),
                ('task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='edge_samples', to='configuration.acqtask')),
            ],
            options={
                'ordering': ('edge', 'task', 'point_code'),
            },
        ),
        migrations.AddIndex(
            model_name='edgelifecycleevent',
            index=models.Index(fields=['edge', '-received_at'], name='edge_lifecycle_idx'),
        ),
        migrations.AddIndex(
            model_name='edgesample',
            index=models.Index(fields=['edge', 'task'], name='edge_sample_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='edgesample',
            unique_together={('edge', 'task', 'point_code')},
        ),
    ]

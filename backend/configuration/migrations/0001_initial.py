from django.db import migrations, models
import django.db.models.deletion
import decimal


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Site",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=128)),
                ("description", models.TextField(blank=True)),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.CreateModel(
            name="AcqTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=128)),
                ("description", models.TextField(blank=True)),
                ("schedule", models.CharField(default="continuous", max_length=64)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.CreateModel(
            name="ConfigVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version", models.PositiveIntegerField()),
                ("summary", models.TextField(blank=True)),
                ("created_by", models.CharField(blank=True, max_length=128)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("task", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="versions", to="configuration.acqtask")),
            ],
            options={"ordering": ["-created_at"], "unique_together": {("task", "version")}},
        ),
        migrations.CreateModel(
            name="Device",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=128)),
                ("code", models.CharField(max_length=128)),
                ("protocol", models.CharField(max_length=32)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("port", models.PositiveIntegerField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("site", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="devices", to="configuration.site")),
            ],
            options={"ordering": ["site", "code"], "unique_together": {("site", "code")}},
        ),
        migrations.CreateModel(
            name="WorkerEndpoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("identifier", models.CharField(max_length=128, unique=True)),
                ("host", models.CharField(max_length=128)),
                ("description", models.CharField(blank=True, max_length=255)),
                ("status", models.CharField(default="unknown", max_length=32)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
            ],
            options={"ordering": ["identifier"]},
        ),
        migrations.CreateModel(
            name="Channel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=64)),
                ("number", models.PositiveIntegerField()),
                ("sampling_rate_hz", models.DecimalField(decimal_places=2, default=decimal.Decimal("1.00"), max_digits=8)),
                ("config", models.JSONField(blank=True, default=dict)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="channels", to="configuration.device")),
            ],
            options={"ordering": ["device", "number"], "unique_together": {("device", "number")}},
        ),
        migrations.CreateModel(
            name="PointTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=128)),
                ("english_name", models.CharField(max_length=128)),
                ("unit", models.CharField(blank=True, max_length=32)),
                ("data_type", models.CharField(default="float", max_length=32)),
                ("coefficient", models.DecimalField(decimal_places=4, default=decimal.Decimal("1.0000"), max_digits=12)),
                ("precision", models.PositiveSmallIntegerField(default=2)),
            ],
            options={"ordering": ["name"], "unique_together": {("name", "english_name")}},
        ),
        migrations.CreateModel(
            name="Point",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.CharField(max_length=128)),
                ("address", models.CharField(max_length=128)),
                ("description", models.CharField(blank=True, max_length=255)),
                ("sample_rate_hz", models.DecimalField(decimal_places=2, default=decimal.Decimal("1.00"), max_digits=8)),
                ("to_kafka", models.BooleanField(default=False)),
                ("extra", models.JSONField(blank=True, default=dict)),
                ("channel", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="points", to="configuration.channel")),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="points", to="configuration.device")),
                ("template", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="points", to="configuration.pointtemplate")),
            ],
            options={"ordering": ["device", "code"], "unique_together": {("device", "code")}},
        ),
        migrations.CreateModel(
            name="TaskPoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("overrides", models.JSONField(blank=True, default=dict)),
                ("point", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="configuration.point")),
                ("task", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="configuration.acqtask")),
            ],
            options={"unique_together": {("task", "point")}},
        ),
        migrations.CreateModel(
            name="ImportJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("source_name", models.CharField(max_length=255)),
                ("triggered_by", models.CharField(blank=True, max_length=128)),
                ("status", models.CharField(choices=[("pending", "???"), ("validated", "???"), ("failed", "??"), ("applied", "???")], default="pending", max_length=16)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("related_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="import_jobs", to="configuration.configversion")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="TaskRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("pending", "???"), ("running", "???"), ("succeeded", "??"), ("failed", "??"), ("stopped", "???")], default="pending", max_length=16)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("log_reference", models.CharField(blank=True, max_length=255)),
                ("context", models.JSONField(blank=True, default=dict)),
                ("config_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="runs", to="configuration.configversion")),
                ("task", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="runs", to="configuration.acqtask")),
                ("worker", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="runs", to="configuration.workerendpoint")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddField(
            model_name="acqtask",
            name="points",
            field=models.ManyToManyField(related_name="tasks", through="configuration.TaskPoint", to="configuration.point"),
        ),
    ]

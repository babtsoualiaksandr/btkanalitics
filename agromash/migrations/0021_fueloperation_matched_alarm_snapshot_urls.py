from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0020_telegramsubscriber_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="fueloperation",
            name="matched_alarm_snapshot_urls",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Список URL/путей на снимки Alarm.original_quality_snapshot для matched_alarms (best-effort).",
            ),
        ),
    ]


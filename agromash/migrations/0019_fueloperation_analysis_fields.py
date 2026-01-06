from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0018_plateidentity_and_backfill"),
    ]

    operations = [
        migrations.AddField(
            model_name="fueloperation",
            name="plate_identity",
            field=models.ForeignKey(
                blank=True,
                help_text="Связанный PlateIdentity, подобранный по card_number -> owner_middle_name",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fuel_operations",
                to="agromash.plateidentity",
            ),
        ),
        migrations.AddField(
            model_name="fueloperation",
            name="matched_alarms",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Список совпавших Alarm (в пределах окна времени). Формат: [{id, alarm_id, start_time, start_time_iso, delta_seconds}]",
            ),
        ),
        migrations.AddField(
            model_name="fueloperation",
            name="analyzed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]


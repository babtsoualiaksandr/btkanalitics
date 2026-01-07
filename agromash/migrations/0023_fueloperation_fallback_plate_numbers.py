from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0022_fuelreport_export_xlsx_cache"),
    ]

    operations = [
        migrations.AddField(
            model_name="fueloperation",
            name="fallback_plate_numbers",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Если PlateIdentity не подобран по card_number -> owner_middle_name — "
                    "список уникальных распознанных номеров, извлечённых из Alarm.plate_identities "
                    "для matched_alarms."
                ),
            ),
        ),
    ]


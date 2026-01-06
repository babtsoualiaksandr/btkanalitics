from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0021_fueloperation_matched_alarm_snapshot_urls"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelreport",
            name="export_xlsx_status",
            field=models.CharField(
                choices=[
                    ("none", "Not generated"),
                    ("pending", "Generating"),
                    ("ready", "Ready"),
                    ("error", "Error"),
                ],
                db_index=True,
                default="none",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="fuelreport",
            name="export_xlsx_task_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="fuelreport",
            name="export_xlsx_generated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fuelreport",
            name="export_xlsx_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="fuelreport",
            name="export_xlsx_content",
            field=models.BinaryField(blank=True, editable=False, null=True),
        ),
    ]


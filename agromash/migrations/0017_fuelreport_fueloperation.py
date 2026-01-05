from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0016_telegramreportsubscription_email_reportrunlog_channel"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="FuelReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("title", models.CharField(blank=True, max_length=255)),
                ("contract_number", models.CharField(blank=True, db_index=True, max_length=255)),
                ("organization_name", models.CharField(blank=True, max_length=500)),
                ("period_start", models.DateField(blank=True, db_index=True, null=True)),
                ("period_end", models.DateField(blank=True, db_index=True, null=True)),
                ("source_filename", models.CharField(blank=True, max_length=255)),
                ("source_sha256", models.CharField(blank=True, db_index=True, max_length=64)),
                ("rows_count", models.PositiveIntegerField(default=0)),
                ("imported_ok", models.BooleanField(db_index=True, default=True)),
                ("import_error", models.TextField(blank=True)),
                (
                    "imported_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fuel_reports",
                        to="auth.user",
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="FuelOperation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("card_number", models.CharField(db_index=True, max_length=32)),
                ("department_number", models.CharField(blank=True, db_index=True, max_length=64)),
                ("operation_at", models.DateTimeField(db_index=True)),
                ("product_name", models.CharField(blank=True, max_length=255)),
                ("product_code", models.CharField(blank=True, db_index=True, max_length=64)),
                ("quantity", models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True)),
                ("unit", models.CharField(blank=True, max_length=32)),
                ("unit_price", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("cost", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("vat", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("discount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("service_percent", models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True)),
                ("service_cost", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("total_cost", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("total_vat", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("station_owner", models.CharField(blank=True, max_length=255)),
                ("station_number", models.CharField(blank=True, max_length=64)),
                ("pump_section", models.CharField(blank=True, max_length=64)),
                ("driver_name", models.CharField(blank=True, max_length=255)),
                ("vehicle_number", models.CharField(blank=True, max_length=64)),
                (
                    "report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="operations",
                        to="agromash.fuelreport",
                    ),
                ),
            ],
            options={
                "ordering": ("-operation_at",),
            },
        ),
        migrations.AddIndex(
            model_name="fueloperation",
            index=models.Index(fields=["card_number", "operation_at"], name="agromash_fue_card_n_1dfc15_idx"),
        ),
    ]


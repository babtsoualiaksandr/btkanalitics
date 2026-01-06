from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0019_fueloperation_analysis_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegramsubscriber",
            name="email",
            field=models.EmailField(
                blank=True,
                null=True,
                db_index=True,
                max_length=254,
                verbose_name="Email",
                help_text="Если задан — может использоваться для ручной отправки отчётов.",
            ),
        ),
    ]


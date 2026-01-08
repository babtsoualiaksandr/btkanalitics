from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0023_fueloperation_fallback_plate_numbers"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegramsubscribermonitorsubscription",
            name="enabled",
            field=models.BooleanField(
                default=True,
                db_index=True,
                help_text="Если выключено — оповещения по Alarm для этого монитора подписчику не отправляются.",
                verbose_name="Активно",
            ),
        ),
    ]


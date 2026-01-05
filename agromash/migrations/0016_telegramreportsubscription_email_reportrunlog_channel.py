from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0015_telegrameventlog_reportrunlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegramreportsubscription",
            name="email",
            field=models.EmailField(
                blank=True,
                db_index=True,
                help_text="Если задан — отчёт будет отправляться также на email.",
                max_length=254,
                null=True,
                verbose_name="Email получателя",
            ),
        ),
        migrations.AddField(
            model_name="reportrunlog",
            name="channel",
            field=models.CharField(
                choices=[("telegram", "Telegram"), ("email", "Email")],
                db_index=True,
                default="telegram",
                max_length=16,
            ),
        ),
    ]


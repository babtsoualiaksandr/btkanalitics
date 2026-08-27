# Data-миграция: перешифровывает существующие plaintext-пароли
# AccountVideoAnalytics.password после смены типа поля на EncryptedCharField
# (0030).
#
# Историческая модель здесь уже использует EncryptedCharField, поэтому
# account.password при чтении уже прошёл через from_db_value(): для ещё
# не мигрированного plaintext-значения оно возвращается как есть (см.
# agromash/fields.py — толерантный fallback на ValueError/InvalidToken),
# а для уже зашифрованного — корректно расшифровывается. В обоих случаях
# `account.password` в этой миграции — исходный plaintext. Просто
# re-save через .update() заново шифрует его (get_prep_value вызывается
# и для QuerySet.update()) — идемпотентно при повторном запуске.

from django.db import migrations


def encrypt_existing_passwords(apps, schema_editor):
    AccountVideoAnalytics = apps.get_model('agromash', 'AccountVideoAnalytics')
    for account in AccountVideoAnalytics.objects.all():
        if not account.password:
            continue
        AccountVideoAnalytics.objects.filter(pk=account.pk).update(password=account.password)


def decrypt_back_to_plaintext(apps, schema_editor):
    """Обратная миграция: записать расшифрованный plaintext через RAW SQL.

    ВАЖНО: на момент выполнения этой функции поле ещё остаётся
    EncryptedCharField (0030 откатится ПОСЛЕ этой миграции), поэтому
    account.password при чтении уже расшифрован through from_db_value().
    Но обычный .update(password=...) на этом же поле заново зашифровал бы
    значение (get_prep_value всегда шифрует) — поэтому пишем raw SQL
    напрямую, в обход шифрующего get_prep_value, чтобы после отката 0030
    (поле снова станет обычным CharField) в столбце реально лежал plaintext.
    """
    AccountVideoAnalytics = apps.get_model('agromash', 'AccountVideoAnalytics')
    table = AccountVideoAnalytics._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        for account in AccountVideoAnalytics.objects.all():
            if not account.password:
                continue
            cursor.execute(
                f"UPDATE {table} SET password = %s WHERE id = %s",
                [account.password, account.pk],
            )


class Migration(migrations.Migration):

    dependencies = [
        ('agromash', '0030_alter_accountvideoanalytics_password'),
    ]

    operations = [
        migrations.RunPython(encrypt_existing_passwords, decrypt_back_to_plaintext),
    ]

from django.db import migrations, models
import django.db.models.deletion


def _normalize_number(value: str) -> str:
    return (value or "").strip().upper().replace(" ", "")


def backfill_plate_identities(apps, schema_editor):
    Alarm = apps.get_model("agromash", "Alarm")
    PlateIdentity = apps.get_model("agromash", "PlateIdentity")

    qs = Alarm.objects.filter(topic="PlateMatched").exclude(plate_identities__isnull=True)

    for alarm in qs.iterator(chunk_size=2000):
        pi = getattr(alarm, "plate_identities", None)
        if not pi or not isinstance(pi, list):
            continue
        for item in pi:
            if not isinstance(item, dict):
                continue
            list_info = item.get("list") or {}
            plates = item.get("plates") or []
            if not isinstance(list_info, dict) or not isinstance(plates, list):
                continue
            for p in plates:
                if not isinstance(p, dict):
                    continue

                number = _normalize_number(str(p.get("number") or ""))
                if not number:
                    continue

                defaults = {
                    "state": str(p.get("state") or "").strip().upper(),
                    "plate_external_id": p.get("id"),
                    "owner_last_name": str(p.get("owner_last_name") or "").strip(),
                    "owner_first_name": str(p.get("owner_first_name") or "").strip(),
                    "owner_middle_name": str(p.get("owner_middle_name") or "").strip(),
                    "list_external_id": list_info.get("id"),
                    "list_name": str(list_info.get("name") or "").strip(),
                    "list_level": list_info.get("level"),
                    "last_alarm_id": alarm.id,
                }

                obj, created = PlateIdentity.objects.get_or_create(number=number, defaults=defaults)
                if created:
                    continue

                # best-effort обновление (не затираем заполненное пустым)
                update = {}
                for k, v in defaults.items():
                    if k == "last_alarm_id":
                        update[k] = v
                        continue
                    if v and not getattr(obj, k):
                        update[k] = v
                if update:
                    PlateIdentity.objects.filter(pk=obj.pk).update(**update)


class Migration(migrations.Migration):

    dependencies = [
        ("agromash", "0017_fuelreport_fueloperation"),
    ]

    operations = [
        migrations.CreateModel(
            name="PlateIdentity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("number", models.CharField(db_index=True, max_length=32, unique=True)),
                ("state", models.CharField(blank=True, max_length=8)),
                ("plate_external_id", models.BigIntegerField(blank=True, null=True)),
                ("owner_last_name", models.CharField(blank=True, max_length=255)),
                ("owner_first_name", models.CharField(blank=True, max_length=255)),
                ("owner_middle_name", models.CharField(blank=True, max_length=255)),
                ("list_external_id", models.BigIntegerField(blank=True, null=True)),
                ("list_name", models.CharField(blank=True, max_length=255)),
                ("list_level", models.IntegerField(blank=True, null=True)),
                (
                    "last_alarm",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="plate_identity_last_seen",
                        to="agromash.alarm",
                    ),
                ),
            ],
            options={
                "ordering": ("number",),
            },
        ),
        migrations.RunPython(backfill_plate_identities, reverse_code=migrations.RunPython.noop),
    ]


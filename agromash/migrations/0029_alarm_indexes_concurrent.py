# Индексы на Alarm.start_time и (monitor_id, start_time) для ускорения
# отчётных запросов (см. agromash/services/reporting.py). Таблица под
# постоянной нагрузкой на запись от SSE-парсеров, поэтому индексы
# создаются через CREATE INDEX CONCURRENTLY (не блокирует таблицу),
# что требует atomic=False и не может выполняться внутри миграции,
# оборачивающей всё в одну транзакцию.

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('agromash', '0028_fuelreport_analysis_status'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='alarm',
            index=models.Index(fields=['start_time'], name='alarm_start_time_idx'),
        ),
        AddIndexConcurrently(
            model_name='alarm',
            index=models.Index(fields=['monitor_id', 'start_time'], name='alarm_monitor_start_idx'),
        ),
    ]

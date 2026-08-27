"""Оркестрация действий над FuelReport: запуск анализа/экспорта/отправки.

Вынесено из agromash/admin/fuel_report.py, чтобы страница
agromash/views_fuel_report.py могла переиспользовать ровно ту же логику
(проверка дублирующей активной задачи, .delay(), обновление статусных
полей), не дублируя её.
"""

import logging
from typing import Optional

from agromash.models import FuelReport
from agromash.tasks import (
    analyze_fuel_report_task,
    generate_fuel_report_xlsx_cache,
    is_task_active,
    send_fuel_report_xlsx_to_subscribers,
)


logger = logging.getLogger(__name__)


def start_analysis(report: FuelReport, *, source: str) -> None:
    """Поставить анализ FuelReport в очередь Celery."""
    async_res = analyze_fuel_report_task.delay(report.id, source=source)
    FuelReport.objects.filter(pk=report.id).update(
        analysis_status=FuelReport.ANALYSIS_STATUS_PENDING,
        analysis_task_id=str(async_res.id),
        analysis_error="",
    )


def start_export(report: FuelReport, *, columns: Optional[list], source: str) -> bool:
    """Поставить формирование XLSX в очередь Celery.

    Возвращает False, если уже есть активная задача (не плодим дубликаты) —
    вызывающий код должен сообщить об этом пользователю и не считать это
    ошибкой.
    """
    task_id = str(getattr(report, "export_xlsx_task_id", "") or "").strip()
    if task_id and is_task_active(task_id):
        return False

    async_res = generate_fuel_report_xlsx_cache.delay(report.id, columns, source=source)
    FuelReport.objects.filter(pk=report.id).update(
        export_xlsx_status=FuelReport.EXPORT_STATUS_PENDING,
        export_xlsx_task_id=async_res.id,
        export_xlsx_error="",
        export_xlsx_content=None,
        export_xlsx_generated_at=None,
    )
    return True


def send_to_subscribers(
    report: FuelReport,
    *,
    subscriber_ids: list,
    columns: Optional[list],
    source: str,
) -> None:
    """Поставить отправку XLSX выбранным подписчикам в очередь Celery."""
    send_fuel_report_xlsx_to_subscribers.delay(report.id, subscriber_ids, columns, source=source)

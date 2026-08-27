"""Отдельная (не-admin) страница для операторской работы с отчётами о заправках:
загрузка XLSX, запуск анализа, скачивание/пересоздание отчёта, отправка
подписчикам. Вся тяжёлая логика — в agromash/services/fuel_report_*.py и
agromash/tasks.py, здесь только view-слой (см. agromash/admin/fuel_report.py
для admin-версии того же функционала — оба используют одни и те же
agromash/services/fuel_report_actions.py, чтобы не дублировать поведение).
"""

import logging

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .models import FuelReport, TelegramSubscriber
from .services.fuel_report_actions import send_to_subscribers, start_analysis, start_export
from .services.fuel_report_exporter import FUEL_REPORT_XLSX_COLUMNS
from .services.fuel_report_importer import FuelImportError, import_fuel_report_from_xlsx
from .views import get_running_parsers_liveness


logger = logging.getLogger(__name__)


class FuelReportImportForm(forms.Form):
    xlsx_file = forms.FileField(label="XLSX файл", required=True)
    period_start = forms.DateField(label="Период с", required=False)
    period_end = forms.DateField(label="Период по", required=False)


@staff_member_required
@require_GET
def fuel_report_list(request):
    reports = FuelReport.objects.all().defer("export_xlsx_content")
    xlsx_columns = [
        {
            "key": str(c.get("key")),
            "label": str(c.get("header")),
            "default": bool(c.get("default")),
        }
        for c in (FUEL_REPORT_XLSX_COLUMNS or [])
        if c.get("key") and c.get("header")
    ]
    ctx = {
        "reports": reports,
        "import_form": FuelReportImportForm(),
        "telegram_subscribers": TelegramSubscriber.objects.all().order_by("username", "chat_id"),
        "xlsx_columns": xlsx_columns,
        "running_parsers": get_running_parsers_liveness(),
    }
    return render(request, "agromash/fuel_reports.html", ctx)


@staff_member_required
@require_POST
def fuel_report_upload(request):
    form = FuelReportImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, f"Некорректная форма загрузки: {form.errors.as_text()}")
        return redirect("fuel_report_list")

    f = form.cleaned_data["xlsx_file"]
    try:
        res = import_fuel_report_from_xlsx(
            file_obj=f,
            filename=getattr(f, "name", ""),
            imported_by=request.user,
            period_start=form.cleaned_data.get("period_start"),
            period_end=form.cleaned_data.get("period_end"),
        )
    except FuelImportError as e:
        messages.error(request, f"Ошибка импорта XLSX: {e}")
        return redirect("fuel_report_list")
    except Exception:
        logger.exception("FuelReport import failed (fuel_report_upload)")
        messages.error(request, "Ошибка импорта XLSX (см. логи)")
        return redirect("fuel_report_list")

    messages.success(
        request,
        f"Импорт выполнен: отчёт #{res.report.id}, строк={res.created_rows}, пропущено={res.skipped_rows}",
    )
    return redirect("fuel_report_list")


@staff_member_required
@require_POST
def fuel_report_analyze(request, report_id: int):
    report = get_object_or_404(FuelReport, pk=report_id)
    try:
        start_analysis(report, source="fuel_reports_page")
        messages.success(request, f"Анализ отчёта #{report.id} поставлен в очередь")
    except Exception:
        logger.exception("Failed to enqueue analysis (report_id=%s)", report.id)
        messages.error(request, "Не удалось поставить анализ в очередь — см. логи")
    return redirect("fuel_report_list")


@staff_member_required
@require_POST
def fuel_report_export(request, report_id: int):
    report = get_object_or_404(FuelReport, pk=report_id)
    columns = request.POST.getlist("columns")
    try:
        started = start_export(report, columns=columns, source="fuel_reports_page")
    except Exception:
        logger.exception("Failed to enqueue export (report_id=%s)", report.id)
        messages.error(request, "Не удалось поставить формирование XLSX в очередь — см. логи")
        return redirect("fuel_report_list")

    if not started:
        messages.warning(request, f"XLSX уже формируется (task_id={report.export_xlsx_task_id})")
    else:
        messages.success(request, f"Формирование XLSX для отчёта #{report.id} поставлено в очередь")
    return redirect("fuel_report_list")


@staff_member_required
@require_GET
def fuel_report_download(request, report_id: int):
    report = get_object_or_404(FuelReport, pk=report_id)
    if report.export_xlsx_status == FuelReport.EXPORT_STATUS_READY and report.export_xlsx_content:
        resp = HttpResponse(
            report.export_xlsx_content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="fuel_report_{report.id}.xlsx"'
        return resp

    messages.warning(request, "XLSX ещё не сформирован — нажмите «Сформировать XLSX» и обновите страницу")
    return redirect("fuel_report_list")


@staff_member_required
@require_POST
def fuel_report_send(request, report_id: int):
    report = get_object_or_404(FuelReport, pk=report_id)

    subscriber_ids = []
    for v in request.POST.getlist("subscriber_ids"):
        try:
            subscriber_ids.append(int(v))
        except (TypeError, ValueError):
            continue

    if not subscriber_ids:
        messages.error(request, "Не выбраны получатели")
        return redirect("fuel_report_list")

    columns = request.POST.getlist("columns")
    try:
        send_to_subscribers(report, subscriber_ids=subscriber_ids, columns=columns, source="fuel_reports_page")
        messages.success(request, f"Отправка XLSX для отчёта #{report.id} поставлена в очередь")
    except Exception:
        logger.exception("Failed to enqueue send (report_id=%s)", report.id)
        messages.error(request, "Не удалось поставить отправку в очередь — см. логи")
    return redirect("fuel_report_list")

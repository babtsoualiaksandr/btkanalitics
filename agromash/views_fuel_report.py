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
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from .models import Alarm, FuelOperation, FuelReport, TelegramSubscriber
from .services.fuel_report_actions import send_to_subscribers, start_analysis, start_export
from .services.fuel_report_exporter import FUEL_REPORT_XLSX_COLUMNS
from .services.fuel_report_importer import FuelImportError, import_fuel_report_from_xlsx
from .views import get_running_parsers_liveness


logger = logging.getLogger(__name__)

OPERATIONS_LIST_LIMIT = 500
REPORTS_PAGE_SIZE = 20


class FuelReportImportForm(forms.Form):
    xlsx_file = forms.FileField(label="XLSX файл", required=True)
    period_start = forms.DateField(label="Период с", required=False)
    period_end = forms.DateField(label="Период по", required=False)


@staff_member_required
@require_GET
def fuel_report_list(request):
    all_reports = FuelReport.objects.all().defer("export_xlsx_content")
    stats = {
        "total": all_reports.count(),
        "analysis_pending": all_reports.filter(analysis_status=FuelReport.ANALYSIS_STATUS_PENDING).count(),
        "export_ready": all_reports.filter(export_xlsx_status=FuelReport.EXPORT_STATUS_READY).count(),
    }

    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()
    date_from = parse_date(date_from_raw)
    date_to = parse_date(date_to_raw)

    filtered_reports = all_reports
    if date_from:
        filtered_reports = filtered_reports.filter(created_at__date__gte=date_from)
    if date_to:
        filtered_reports = filtered_reports.filter(created_at__date__lte=date_to)

    paginator = Paginator(filtered_reports, REPORTS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

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
        "reports": page_obj,
        "page_obj": page_obj,
        "date_from": date_from_raw,
        "date_to": date_to_raw,
        "stats": stats,
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
@require_GET
def fuel_report_operations(request, report_id: int):
    """Фрагмент HTML со списком строк отчёта (для показа в диалоге на странице списка)."""
    report = get_object_or_404(FuelReport, pk=report_id)
    ops_qs = FuelOperation.objects.filter(report_id=report.id).select_related("plate_identity")
    total_ops = ops_qs.count()
    operations = list(ops_qs.order_by("-operation_at")[:OPERATIONS_LIST_LIMIT])

    # matched_alarms кэшируется в момент анализа (см. fuel_report_analyzer.py) и
    # содержит только snapshot_url на момент анализа — video_clip к тому моменту
    # мог ещё не скачаться (задача асинхронная) или монитор мог быть включён на
    # запись позже. Поэтому video-доступность подтягиваем свежей отдельным запросом.
    alarm_pks: set[int] = set()
    for op in operations:
        for row in (op.matched_alarms or []):
            pk = row.get("id")
            if pk:
                alarm_pks.add(pk)

    alarms_by_pk = {}
    if alarm_pks:
        alarms_by_pk = {
            a.id: a
            for a in Alarm.objects.filter(pk__in=alarm_pks).only(
                "id", "alarm_id", "video_clip", "video_clip_status"
            )
        }

    for op in operations:
        enriched = []
        for row in (op.matched_alarms or []):
            alarm = alarms_by_pk.get(row.get("id"))
            item = dict(row)
            item["snapshot_view_url"] = (
                reverse("serve_snapshot", args=[alarm.alarm_id]) if alarm else None
            )
            item["video_view_url"] = (
                reverse("serve_alarm_video", args=[alarm.alarm_id])
                if alarm and alarm.video_clip and alarm.video_clip_status == Alarm.VIDEO_STATUS_READY
                else None
            )
            enriched.append(item)
        op.matched_alarms_view = enriched

    ctx = {
        "report": report,
        "operations": operations,
        "total_ops": total_ops,
        "shown_ops": min(total_ops, OPERATIONS_LIST_LIMIT),
    }
    return render(request, "agromash/_fuel_report_operations.html", ctx)


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

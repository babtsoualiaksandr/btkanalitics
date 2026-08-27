import logging

from django.contrib import admin, messages
from django import forms
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from ..models import FuelOperation, FuelReport, TelegramSubscriber
from ..services.fuel_report_actions import send_to_subscribers, start_analysis, start_export
from ..services.fuel_report_exporter import FUEL_REPORT_XLSX_COLUMNS
from ..services.fuel_report_importer import FuelImportError, import_fuel_report_from_xlsx


logger = logging.getLogger(__name__)


class FuelReportImportForm(forms.Form):
    xlsx_file = forms.FileField(label="XLSX файл", required=True)
    period_start = forms.DateField(label="Период с", required=False)
    period_end = forms.DateField(label="Период по", required=False)


@admin.register(FuelReport)
class FuelReportAdmin(admin.ModelAdmin):
    change_list_template = "admin/agromash/fuelreport/change_list.html"

    list_display = (
        "id",
        "created_at",
        "contract_number",
        "organization_name",
        "period_start",
        "period_end",
        "rows_count",
        "imported_ok",
    )
    list_filter = ("imported_ok", "period_start", "period_end")
    search_fields = ("contract_number", "organization_name", "source_filename", "source_sha256")
    readonly_fields = (
        "created_at",
        "rows_count",
        "imported_ok",
        "import_error",
        "source_sha256",
        "analysis_status",
        "analysis_task_id",
        "analysis_error",
        "analysis_finished_at",
        "export_xlsx_status",
        "export_xlsx_generated_at",
        "export_xlsx_task_id",
        "export_xlsx_error",
    )

    def get_queryset(self, request):
        # В списке админки не вытаскиваем большие bytes XLSX из БД.
        return super().get_queryset(request).defer("export_xlsx_content")

    def get_list_display(self, request):
        base = super().get_list_display(request)

        def analyze_controls(obj: FuelReport):
            return self._analyze_controls(request, obj)

        def download_controls(obj: FuelReport):
            return self._download_controls(request, obj)

        def send_controls(obj: FuelReport):
            return self._send_controls(request, obj)

        analyze_controls.short_description = "Анализ"
        download_controls.short_description = "Отчёт"
        send_controls.short_description = "Отправка"
        return (*base, analyze_controls, download_controls, send_controls)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "import-xlsx/",
                self.admin_site.admin_view(self.import_xlsx_view),
                name="agromash_fuelreport_import_xlsx",
            ),
            path(
                "<path:object_id>/run-analysis/",
                self.admin_site.admin_view(self.run_analysis_view),
                name="agromash_fuelreport_run_analysis",
            ),
            path(
                "<path:object_id>/download-xlsx/",
                self.admin_site.admin_view(self.download_xlsx_view),
                name="agromash_fuelreport_download_xlsx",
            ),
            path(
                "<path:object_id>/enqueue-xlsx/",
                self.admin_site.admin_view(self.enqueue_xlsx_view),
                name="agromash_fuelreport_enqueue_xlsx",
            ),
            path(
                "<path:object_id>/send-xlsx/",
                self.admin_site.admin_view(self.send_xlsx_view),
                name="agromash_fuelreport_send_xlsx",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        # Список подписчиков для модального окна отправки
        extra_context["telegram_subscribers"] = (
            TelegramSubscriber.objects.all().order_by("username", "chat_id")
        )
        extra_context["xlsx_columns"] = [
            {
                "key": str(c.get("key")),
                "label": str(c.get("header")),
                "default": bool(c.get("default")),
            }
            for c in (FUEL_REPORT_XLSX_COLUMNS or [])
            if c.get("key") and c.get("header")
        ]
        # URL-шаблон, чтобы JS мог подставить report_id
        extra_context["send_xlsx_url_template"] = reverse(
            "admin:agromash_fuelreport_send_xlsx",
            args=["__id__"],
        )
        extra_context["enqueue_xlsx_url_template"] = reverse(
            "admin:agromash_fuelreport_enqueue_xlsx",
            args=["__id__"],
        )
        return super().changelist_view(request, extra_context=extra_context)

    def _analyze_controls(self, request, obj: FuelReport):
        run_url = reverse("admin:agromash_fuelreport_run_analysis", args=[obj.pk])
        status = getattr(obj, "analysis_status", FuelReport.ANALYSIS_STATUS_NONE)
        task_id = getattr(obj, "analysis_task_id", "") or ""

        if status == FuelReport.ANALYSIS_STATUS_PENDING:
            return format_html(
                '<div class="agromash-admin-action js-task-poll" '
                'data-report-id="{}" data-task-id="{}" data-task-type="analysis">'
                '<button type="button" class="button" disabled>'
                '<span class="js-task-label">Анализ…</span></button>'
                '<span class="agromash-admin-action-hint js-task-hint">в очереди</span>'
                '</div>',
                obj.pk, task_id,
            )

        if status == FuelReport.ANALYSIS_STATUS_DONE:
            hint = "✅ готово"
            if getattr(obj, "analysis_finished_at", None):
                hint = f"✅ {timezone.localtime(obj.analysis_finished_at).strftime('%H:%M:%S')}"
            return format_html(
                '<div class="agromash-admin-action">'
                '<button type="submit" class="button" formaction="{}" formmethod="post" '
                'onclick="return confirm(\'Перезапустить анализ? Текущий результат будет заменён.\');">Перезапустить</button>'
                '<span class="agromash-admin-action-hint">{}</span>'
                '</div>',
                run_url, hint,
            )

        if status == FuelReport.ANALYSIS_STATUS_ERROR:
            return format_html(
                '<div class="agromash-admin-action">'
                '<button type="submit" class="button" formaction="{}" formmethod="post" '
                'onclick="return confirm(\'Повторить анализ после ошибки?\');">Повторить</button>'
                '<span class="agromash-admin-action-hint" style="color:#a61e1e;">❌ ошибка</span>'
                '</div>',
                run_url,
            )

        # none / default
        return format_html(
            '<div class="agromash-admin-action">'
            '<button type="submit" class="button" formaction="{}" formmethod="post">Запустить</button>'
            '<span class="agromash-admin-action-hint"></span>'
            '</div>',
            run_url,
        )

    def run_analysis_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        report: FuelReport = self.get_object(request, object_id)
        if report is None:
            raise Http404("FuelReport not found")

        try:
            start_analysis(report, source="admin")
            self.message_user(
                request,
                f"Анализ поставлен в очередь Celery (report_id={report.id})",
                level=messages.SUCCESS,
            )
        except Exception:
            logger.exception("Failed to enqueue analyze_fuel_report_task (report_id=%s)", report.id)
            self.message_user(
                request,
                f"Не удалось поставить анализ в очередь Celery (report_id={report.id}) — см. логи",
                level=messages.ERROR,
            )

        return redirect(request.META.get("HTTP_REFERER") or reverse("admin:agromash_fuelreport_changelist"))

    def _download_controls(self, request, obj: FuelReport):
        status = getattr(obj, "export_xlsx_status", FuelReport.EXPORT_STATUS_NONE)
        download_url = reverse("admin:agromash_fuelreport_download_xlsx", args=[obj.pk])
        enqueue_url = reverse("admin:agromash_fuelreport_enqueue_xlsx", args=[obj.pk])

        if status == FuelReport.EXPORT_STATUS_READY and getattr(obj, "export_xlsx_generated_at", None):
            return format_html(
                '<div class="agromash-admin-action">'
                '<a class="button" href="{}">Скачать XLSX</a>'
                '<button type="button" class="button js-fuelreport-enqueue" data-report-id="{}">Пересоздать…</button>'
                '<span class="agromash-admin-action-hint">готово</span>'
                '</div>',
                download_url,
                obj.pk,
            )

        if status == FuelReport.EXPORT_STATUS_PENDING and getattr(obj, "export_xlsx_task_id", None):
            return format_html(
                '<div class="agromash-admin-action js-task-poll" '
                'data-report-id="{}" data-task-id="{}" data-task-type="export">'
                '<button type="button" class="button" disabled>'
                '<span class="js-task-label">Генерация…</span></button>'
                '<span class="agromash-admin-action-hint js-task-hint">в очереди</span>'
                '</div>',
                obj.pk, getattr(obj, "export_xlsx_task_id", ""),
            )

        # none/error → предлагаем сгенерировать в фоне
        suffix_html = "" if status != FuelReport.EXPORT_STATUS_ERROR else "ошибка"
        return format_html(
            '<div class="agromash-admin-action">'
            '<button type="button" class="button js-fuelreport-enqueue" data-report-id="{}">Сформировать XLSX</button>'
            '<span class="agromash-admin-action-hint">{}</span>'
            '</div>',
            obj.pk,
            suffix_html,
        )

    def enqueue_xlsx_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        report: FuelReport = self.get_object(request, object_id)
        if report is None:
            raise Http404("FuelReport not found")

        columns = request.POST.getlist("columns")
        try:
            started = start_export(report, columns=columns, source="admin")
        except Exception:
            logger.exception("Failed to enqueue generate_fuel_report_xlsx_cache (report_id=%s)", report.id)
            self.message_user(request, "Не удалось поставить задачу в очередь Celery — см. логи", level=messages.ERROR)
            return redirect(request.META.get("HTTP_REFERER") or reverse("admin:agromash_fuelreport_changelist"))

        if not started:
            self.message_user(
                request,
                f"XLSX уже формируется (task_id={report.export_xlsx_task_id})",
                level=messages.WARNING,
            )
            return redirect(request.META.get("HTTP_REFERER") or reverse("admin:agromash_fuelreport_changelist"))

        self.message_user(
            request,
            f"Формирование XLSX поставлено в очередь Celery (report_id={report.id})",
            level=messages.SUCCESS,
        )
        return redirect(request.META.get("HTTP_REFERER") or reverse("admin:agromash_fuelreport_changelist"))

    def _send_controls(self, request, obj: FuelReport):
        # Кнопка открывает модальное окно в changelist (см. шаблон)
        return format_html(
            '<div class="agromash-admin-action">'
            '<button type="button" class="button js-fuelreport-send" data-report-id="{}">Отправить…</button>'
            '<span class="agromash-admin-action-hint"></span>'
            '</div>',
            obj.pk,
        )

    def send_xlsx_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        report: FuelReport = self.get_object(request, object_id)
        if report is None:
            raise Http404("FuelReport not found")

        raw_ids = request.POST.getlist("subscriber_ids")
        subscriber_ids = []
        for v in raw_ids:
            try:
                subscriber_ids.append(int(v))
            except Exception:
                continue

        if not subscriber_ids:
            self.message_user(request, "Не выбраны получатели", level=messages.ERROR)
            return redirect(request.META.get("HTTP_REFERER") or reverse("admin:agromash_fuelreport_changelist"))

        columns = request.POST.getlist("columns")

        try:
            send_to_subscribers(report, subscriber_ids=subscriber_ids, columns=columns, source="admin")
            self.message_user(
                request,
                f"Отправка XLSX поставлена в очередь Celery (report_id={report.id})",
                level=messages.SUCCESS,
            )
        except Exception:
            logger.exception("Failed to enqueue send_fuel_report_xlsx_to_subscribers (report_id=%s)", report.id)
            self.message_user(request, "Не удалось поставить задачу в очередь Celery — см. логи", level=messages.ERROR)

        return redirect(request.META.get("HTTP_REFERER") or reverse("admin:agromash_fuelreport_changelist"))

    def download_xlsx_view(self, request, object_id):
        # для скачивания достаточно GET
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])

        report: FuelReport = self.get_object(request, object_id)
        if report is None:
            raise Http404("FuelReport not found")

        # Если файл уже сгенерирован в фоне — отдаём его без пересчёта.
        if (
            getattr(report, "export_xlsx_status", None) == FuelReport.EXPORT_STATUS_READY
            and getattr(report, "export_xlsx_content", None)
        ):
            filename = f"fuel_report_{report.id}.xlsx"
            resp = HttpResponse(
                report.export_xlsx_content,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            resp["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp

        # Иначе не блокируем HTTP — просим сначала сгенерировать.
        self.message_user(request, "XLSX ещё не сформирован — нажмите «Сформировать XLSX» и обновите страницу", level=messages.WARNING)
        return redirect(request.META.get("HTTP_REFERER") or reverse("admin:agromash_fuelreport_changelist"))

    def import_xlsx_view(self, request):
        if request.method == "GET":
            form = FuelReportImportForm()
            ctx = admin.site.each_context(request)
            ctx.update({"title": "Импорт пооперационного отчёта (XLSX)", "form": form})
            return TemplateResponse(request, "admin/agromash/fuelreport/import_xlsx.html", ctx)

        if request.method != "POST":
            return HttpResponseNotAllowed(["GET", "POST"])

        form = FuelReportImportForm(request.POST, request.FILES)
        if not form.is_valid():
            ctx = admin.site.each_context(request)
            ctx.update({"title": "Импорт пооперационного отчёта (XLSX)", "form": form})
            return TemplateResponse(request, "admin/agromash/fuelreport/import_xlsx.html", ctx)

        f = form.cleaned_data["xlsx_file"]
        p_start = form.cleaned_data.get("period_start")
        p_end = form.cleaned_data.get("period_end")

        try:
            res = import_fuel_report_from_xlsx(
                file_obj=f,
                filename=getattr(f, "name", ""),
                imported_by=getattr(request, "user", None),
                period_start=p_start,
                period_end=p_end,
            )
        except FuelImportError as e:
            self.message_user(request, f"Ошибка импорта XLSX: {e}", level=messages.ERROR)
            return redirect(reverse("admin:agromash_fuelreport_changelist"))
        except Exception:
            logger.exception("FuelReport import failed")
            self.message_user(request, "Ошибка импорта XLSX (см. логи)", level=messages.ERROR)
            return redirect(reverse("admin:agromash_fuelreport_changelist"))

        self.message_user(
            request,
            f"Импорт выполнен: report_id={res.report.id}, rows={res.created_rows}, skipped={res.skipped_rows}",
            level=messages.SUCCESS,
        )
        return redirect(reverse("admin:agromash_fuelreport_change", args=[res.report.id]))


class FuelOperationHasPlateIdentityFilter(admin.SimpleListFilter):
    title = "PlateIdentity"
    parameter_name = "has_plate_identity"

    def lookups(self, request, model_admin):
        return (
            ("1", "есть"),
            ("0", "нет"),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == "1":
            return queryset.filter(plate_identity__isnull=False)
        if val == "0":
            return queryset.filter(plate_identity__isnull=True)
        return queryset


class FuelOperationHasMatchedAlarmsFilter(admin.SimpleListFilter):
    title = "matched_alarms"
    parameter_name = "has_matched_alarms"

    def lookups(self, request, model_admin):
        return (
            ("1", "есть"),
            ("0", "нет"),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == "1":
            return queryset.exclude(matched_alarms=[])
        if val == "0":
            return queryset.filter(matched_alarms=[])
        return queryset


class FuelOperationHasFallbackPlateNumbersFilter(admin.SimpleListFilter):
    title = "fallback_plate_numbers"
    parameter_name = "has_fallback_plate_numbers"

    def lookups(self, request, model_admin):
        return (
            ("1", "есть"),
            ("0", "нет"),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == "1":
            return queryset.exclude(fallback_plate_numbers=[])
        if val == "0":
            return queryset.filter(fallback_plate_numbers=[])
        return queryset


@admin.register(FuelOperation)
class FuelOperationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "report",
        "card_number",
        "plate_identity",
        "fallback_plate_numbers_preview",
        "operation_at",
        "product_name",
        "quantity",
        "unit",
        "total_cost",
        "station_owner",
        "station_number",
        "matched_alarms_count",
        "matched_alarms_links",
        "matched_alarm_snapshots_preview",
    )
    list_filter = (
        "report",
        "station_owner",
        "station_number",
        "product_name",
        "plate_identity",
        "analyzed_at",
        FuelOperationHasPlateIdentityFilter,
        FuelOperationHasMatchedAlarmsFilter,
        FuelOperationHasFallbackPlateNumbersFilter,
    )
    search_fields = (
        "card_number",
        "vehicle_number",
        "driver_name",
        "station_number",
        "product_name",
        "product_code",
        "report__contract_number",
    )
    autocomplete_fields = ("report",)
    date_hierarchy = "operation_at"
    list_per_page = 10

    def matched_alarms_count(self, obj: FuelOperation) -> int:
        return len(getattr(obj, "matched_alarms", None) or [])

    matched_alarms_count.short_description = "Alarm (шт)"

    def matched_alarms_links(self, obj: FuelOperation):
        rows = (getattr(obj, "matched_alarms", None) or [])[:5]
        pairs = []
        for row in rows:
            alarm_pk = row.get("id")
            if not alarm_pk:
                continue
            label = row.get("alarm_id") or alarm_pk
            url = reverse("admin:agromash_alarm_change", args=[alarm_pk])
            pairs.append((url, label))

        if not pairs:
            return "-"

        from django.utils.html import format_html_join

        links = format_html_join(", ", '<a href="{}">{}</a>', pairs)
        suffix = "" if len(getattr(obj, "matched_alarms", None) or []) <= 5 else " …"
        return format_html("{}{}", links, suffix)

    matched_alarms_links.short_description = "Alarm"

    def fallback_plate_numbers_preview(self, obj: FuelOperation) -> str:
        nums = getattr(obj, "fallback_plate_numbers", None) or []
        if not nums:
            return "-"
        # чтобы список не раздувал колонку
        import json

        def _fmt(x) -> str:
            if isinstance(x, dict):
                # компактно, но читаемо
                return json.dumps(x, ensure_ascii=False, sort_keys=True)
            return str(x)

        head = " | ".join(_fmt(x) for x in nums[:10])
        suffix = "" if len(nums) <= 10 else " …"
        return head + suffix

    fallback_plate_numbers_preview.short_description = "Fallback номера"

    def matched_alarm_snapshots_preview(self, obj: FuelOperation):
        """Превью снимков для совпавших тревог (через proxy view serve_snapshot).

        `serve_snapshot` сам использует креды из `Alarm.account` (AccountVideoAnalytics).
        """

        rows = (getattr(obj, "matched_alarms", None) or [])
        pairs = []
        for row in rows:
            alarm_id = row.get("alarm_id")
            if not alarm_id:
                continue
            url = reverse("serve_snapshot", args=[alarm_id])
            pairs.append((url, url))

        if not pairs:
            return "-"

        from django.utils.html import format_html_join

        # кликабельные превью
        return format_html_join(
            " ",
            '<a href="{}" target="_blank"><img src="{}" style="height:60px;max-width:120px;object-fit:contain;" /></a>',
            pairs,
        )

    matched_alarm_snapshots_preview.short_description = "Snapshots"

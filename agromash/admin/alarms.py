from django.contrib import admin
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html

from ..models import (
    Alarm,
    AlarmCase,
    AlarmDocument,
    Monitor,
    PlateIdentity,
    UserMonitorAccess,
)
from ..services.common import alarm_epoch_to_aware_dt


@admin.register(Alarm)
class AlarmAdmin(admin.ModelAdmin):
    list_display = (
        'alarm_id',
        'topic',
        'monitor_name',
        'monitor_name_second_token_display',
        'start_time_human',
        'end_time_human',
        'snapshot_preview',
        'video_clip_status',
        'video_clip',
    )
    search_fields = (
        'alarm_id',
        'topic',
        'monitor_name',
        'monitor_id',
        'event_id',
        'account__name',
        'account__organization',
    )
    list_filter = (
        'account',
        'topic',
        'video_clip_status',
    )
    ordering = ('-start_time',)
    list_per_page = 20
    list_max_show_all = 1000
    show_full_result_count = False
    readonly_fields = (
        'data',
        'snapshot_preview',
        'start_time',
        'end_time',
        'start_time_human',
        'end_time_human',
        'video_clip_status',
        'video_clip_size',
        'video_clip_error',
    )

    _to_aware_dt = staticmethod(alarm_epoch_to_aware_dt)

    def start_time_human(self, obj):
        dt = self._to_aware_dt(obj.start_time)
        if not dt:
            return "-"
        return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')

    start_time_human.short_description = 'Start time'
    start_time_human.admin_order_field = 'start_time'

    def end_time_human(self, obj):
        dt = self._to_aware_dt(obj.end_time)
        if not dt:
            return "-"
        return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')

    end_time_human.short_description = 'End time'
    end_time_human.admin_order_field = 'end_time'

    @admin.display(description="Monitor (2-й токен)")
    def monitor_name_second_token_display(self, obj: Alarm) -> str:
        return getattr(obj, "monitor_name_second_token", "")

    def snapshot_preview(self, obj):
        """Отображение превью изображения в списке и форме редактирования"""
        # access_token может отсутствовать при первичном запуске; serve_snapshot сам выполнит login.
        if obj.original_quality_snapshot and obj.account:
            # Используем наш view для отображения изображения
            from django.urls import reverse
            image_url = reverse('serve_snapshot', args=[obj.alarm_id])

            # Создаем HTML для отображения изображения
            return format_html(
                '<img src="{}" style="max-width: 100px; max-height: 100px; object-fit: contain;" '
                'alt="Snapshot" title="Click to view full size" '
                'onclick="window.open(\'{}\', \'_blank\', \'width=800,height=600\')">',
                image_url, image_url
            )
        return "No image available"

    snapshot_preview.short_description = "Snapshot"


@admin.register(PlateIdentity)
class PlateIdentityAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "state",
        "list_name",
        "list_level",
        "owner_last_name",
        "owner_first_name",
        "owner_middle_name",
        "plate_external_id",
        "updated_at",
        "last_alarm",
    )
    search_fields = (
        "number",
        "state",
        "list_name",
        "owner_last_name",
        "owner_first_name",
        "owner_middle_name",
    )
    list_filter = (
        "state",
        "list_level",
    )
    ordering = ("number",)
    readonly_fields = (
        "created_at",
        "updated_at",
    )
    autocomplete_fields = (
        "last_alarm",
    )


@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    list_display = (
        'monitor_id',
        'monitor_name',
        'topic',
        'record_video_enabled',
        'subscribers_count',
        'created_at',
        'updated_at',
    )
    list_editable = ('record_video_enabled',)
    search_fields = (
        'monitor_id',
        'monitor_name',
        'topic',
        'subscribers__chat_id',
        'subscribers__username',
    )
    list_filter = ('topic', 'record_video_enabled')
    readonly_fields = ('created_at', 'updated_at')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(_subscribers_count=Count('subscribers', distinct=True))

    def subscribers_count(self, obj: Monitor):
        return getattr(obj, '_subscribers_count', 0)

    subscribers_count.short_description = 'Подписчиков'
    subscribers_count.admin_order_field = '_subscribers_count'


@admin.register(UserMonitorAccess)
class UserMonitorAccessAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'monitor',
        'enabled',
        'created_at',
    )
    list_filter = (
        'enabled',
        'monitor',
    )
    search_fields = (
        'user__username',
        'user__email',
        'monitor__monitor_id',
        'monitor__monitor_name',
    )
    autocomplete_fields = (
        'user',
        'monitor',
    )

    # Оставляем этот ModelAdmin только для просмотра/поиска (источник истины — форма User).


@admin.register(AlarmCase)
class AlarmCaseAdmin(admin.ModelAdmin):
    list_display = (
        'alarm',
        'created_at',
        'updated_at',
        'created_by',
        'updated_by',
    )
    search_fields = (
        'alarm__alarm_id',
        'alarm__monitor_name',
        'description',
        'note',
    )
    autocomplete_fields = ('alarm', 'created_by', 'updated_by')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AlarmDocument)
class AlarmDocumentAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'case',
        'title',
        'file',
        'uploaded_by',
        'uploaded_at',
    )
    search_fields = (
        'case__alarm__alarm_id',
        'title',
        'file',
    )
    list_filter = ('uploaded_at',)
    autocomplete_fields = ('case', 'uploaded_by')
    readonly_fields = ('uploaded_at',)

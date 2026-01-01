from django.contrib import admin, messages
from django.utils.html import format_html
from django.conf import settings
from django.http import Http404
from django.http import HttpResponseNotAllowed
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.urls import path, reverse
from .models import AccountVideoAnalytics, Alarm, TelegramSubscriber, Monitor

from .tasks import parse_event_task, request_stop_parser


def parse_event_action(modeladmin, request, queryset):
    for account in queryset:
        if account.is_parser_running:
            continue
        async_res = parse_event_task.delay(account.id)
        AccountVideoAnalytics.objects.filter(pk=account.id).update(
            parser_status=AccountVideoAnalytics.PARSER_STATUS_STARTING,
            parser_task_id=async_res.id,
            parser_stop_requested=False,
            parser_last_error=None,
        )

parse_event_action.short_description = "Run parse_event for selected accounts"

@admin.register(AccountVideoAnalytics)
class AccountVideoAnalyticsAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'password',
        'organization',
        'contract',
        'parser_status_badge',
    )
    search_fields = ('name', 'password', 'organization', 'contract')
    actions = [parse_event_action]

    def get_list_display(self, request):
        """Добавляем колонку-кнопку, не сохраняя request в состоянии ModelAdmin (thread-safe)."""
        base = super().get_list_display(request)

        def parser_controls(obj):
            return self._parser_controls(request, obj)

        parser_controls.short_description = 'Parser'
        return (*base, parser_controls)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<path:object_id>/run-parse-event/',
                self.admin_site.admin_view(self.run_parse_event_for_account_view),
                name='agromash_accountvideoanalytics_run_parse_event_for_account',
            ),
            path(
                '<path:object_id>/stop-parse-event/',
                self.admin_site.admin_view(self.stop_parse_event_for_account_view),
                name='agromash_accountvideoanalytics_stop_parse_event_for_account',
            ),
        ]
        return custom_urls + urls

    def parser_status_badge(self, obj: AccountVideoAnalytics):
        status = obj.parser_status
        if obj.is_parser_running:
            status = AccountVideoAnalytics.PARSER_STATUS_RUNNING

        color = {
            AccountVideoAnalytics.PARSER_STATUS_RUNNING: '#1f7a1f',
            AccountVideoAnalytics.PARSER_STATUS_STARTING: '#7a5b1f',
            AccountVideoAnalytics.PARSER_STATUS_STOPPING: '#7a5b1f',
            AccountVideoAnalytics.PARSER_STATUS_ERROR: '#a61e1e',
            AccountVideoAnalytics.PARSER_STATUS_STOPPED: '#444',
        }.get(status, '#444')

        return format_html(
            '<span style="display:inline-block;padding:2px 6px;border-radius:10px;'
            'background:{};color:white;font-size:12px;">{}</span>',
            color,
            status,
        )

    parser_status_badge.short_description = 'Parser status'

    def _parser_controls(self, request, obj: AccountVideoAnalytics):
        """Кнопки запуска/остановки парсера для конкретной записи прямо из списка."""
        csrf = get_token(request)

        run_url = reverse('admin:agromash_accountvideoanalytics_run_parse_event_for_account', args=[obj.pk])
        stop_url = reverse('admin:agromash_accountvideoanalytics_stop_parse_event_for_account', args=[obj.pk])

        if obj.is_parser_running:
            return format_html(
                '<form method="post" action="{}" style="display:inline">'
                '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                '<button type="submit" class="button" style="background:#a61e1e;color:white;">Stop</button>'
                '</form>',
                stop_url,
                csrf,
            )

        return format_html(
            '<form method="post" action="{}" style="display:inline">'
            '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
            '<button type="submit" class="button">Start</button>'
            '</form>',
            run_url,
            csrf,
        )

    def run_parse_event_for_account_view(self, request, object_id):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])

        account = self.get_object(request, object_id)
        if account is None:
            raise Http404('AccountVideoAnalytics not found')

        if account.is_parser_running:
            self.message_user(
                request,
                f'Парсер уже запущен для аккаунта: {account.name}',
                level=messages.WARNING,
            )
            return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_accountvideoanalytics_changelist'))

        async_res = parse_event_task.delay(account.id)

        AccountVideoAnalytics.objects.filter(pk=account.id).update(
            parser_status=AccountVideoAnalytics.PARSER_STATUS_STARTING,
            parser_task_id=async_res.id,
            parser_stop_requested=False,
            parser_last_error=None,
        )

        self.message_user(
            request,
            f'parse_event отправлен в Celery для аккаунта: {account.name} (task_id={async_res.id})',
            level=messages.SUCCESS,
        )

        return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_accountvideoanalytics_changelist'))

    def stop_parse_event_for_account_view(self, request, object_id):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])

        account = self.get_object(request, object_id)
        if account is None:
            raise Http404('AccountVideoAnalytics not found')

        task_id = request_stop_parser(account_id=account.id, terminate=True)
        if task_id:
            self.message_user(
                request,
                f'Остановка парсера запрошена для аккаунта: {account.name} (task_id={task_id})',
                level=messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                f'Остановка парсера запрошена для аккаунта: {account.name}',
                level=messages.SUCCESS,
            )

        return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_accountvideoanalytics_changelist'))


@admin.register(Alarm)
class AlarmAdmin(admin.ModelAdmin):
    list_display = ('alarm_id', 'topic', 'monitor_name', 'start_time', 'end_time', 'snapshot_preview')
    search_fields = ('alarm_id', 'topic', 'monitor_name')
    readonly_fields = ('data', 'snapshot_preview')
    
    def snapshot_preview(self, obj):
        """Отображение превью изображения в списке и форме редактирования"""
        if obj.original_quality_snapshot and obj.account and obj.account.access_token:
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


@admin.register(TelegramSubscriber)
class TelegramSubscriberAdmin(admin.ModelAdmin):
    list_display = ('chat_id', 'username', 'subscribed_at')
    search_fields = ('chat_id', 'username')


@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    list_display = ('monitor_id', 'monitor_name', 'topic', 'created_at', 'updated_at')
    search_fields = ('monitor_id', 'monitor_name', 'topic')
    readonly_fields = ('created_at', 'updated_at')

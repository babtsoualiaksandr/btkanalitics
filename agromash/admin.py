from django.contrib import admin, messages
from django.core.management import call_command
from django.utils.html import format_html
from django.conf import settings
from django.http import Http404
from django.http import HttpResponseNotAllowed
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.urls import path, reverse
from .models import AccountVideoAnalytics, Alarm, TelegramSubscriber, Monitor


def parse_event_action(modeladmin, request, queryset):
    for account in queryset:
        call_command('parse_event', account.name, account.password)

parse_event_action.short_description = "Run parse_event for selected accounts"

@admin.register(AccountVideoAnalytics)
class AccountVideoAnalyticsAdmin(admin.ModelAdmin):
    list_display = ('name', 'password', 'organization', 'contract')
    search_fields = ('name', 'password', 'organization', 'contract')
    actions = [parse_event_action]

    def get_list_display(self, request):
        """Добавляем колонку-кнопку, не сохраняя request в состоянии ModelAdmin (thread-safe)."""
        base = super().get_list_display(request)

        def parse_event_button(obj):
            return self._parse_event_button(request, obj)

        parse_event_button.short_description = 'parse_event'
        return (*base, parse_event_button)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<path:object_id>/run-parse-event/',
                self.admin_site.admin_view(self.run_parse_event_for_account_view),
                name='agromash_accountvideoanalytics_run_parse_event_for_account',
            ),
        ]
        return custom_urls + urls

    def _parse_event_button(self, request, obj):
        """Кнопка запуска parse_event для конкретной записи прямо из списка."""
        url = reverse('admin:agromash_accountvideoanalytics_run_parse_event_for_account', args=[obj.pk])
        csrf = get_token(request)
        return format_html(
            '<form method="post" action="{}" style="display:inline">'
            '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
            '<button type="submit" class="button">parse_event</button>'
            '</form>',
            url,
            csrf,
        )

    def run_parse_event_for_account_view(self, request, object_id):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])

        account = self.get_object(request, object_id)
        if account is None:
            raise Http404('AccountVideoAnalytics not found')

        call_command('parse_event', account.name, account.password)

        self.message_user(
            request,
            f'parse_event запущен для аккаунта: {account.name}',
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

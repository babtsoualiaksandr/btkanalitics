import logging

from django.contrib import admin, messages
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html

from ..models import AccountVideoAnalytics
from ..tasks import parse_event_task, request_stop_parser


logger = logging.getLogger(__name__)


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
        'organization',
        'contract',
        'parser_status_badge',
    )
    search_fields = ('name', 'organization', 'contract')
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
        """Бейдж статуса парсера с учётом heartbeat.

        Важно: если parser_status=running, но heartbeat устарел (>2 мин) —
        is_parser_running вернёт False, и бейдж покажет реальное состояние.

        Рассинхронизация (статус=running, но heartbeat старый) автоматически
        корректируется периодической задачей `agromash.check_parser_heartbeats`.
        """
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
        run_url = reverse('admin:agromash_accountvideoanalytics_run_parse_event_for_account', args=[obj.pk])
        stop_url = reverse('admin:agromash_accountvideoanalytics_stop_parse_event_for_account', args=[obj.pk])

        # ВАЖНО: в changelist Django admin уже есть внешний <form id="changelist-form"> с CSRF.
        # Вложенные <form> внутри таблицы — невалидный HTML и может ломать submit (особенно на последней строке).
        # Поэтому используем HTML5 `formaction`/`formmethod` без вложенных форм.
        if obj.is_parser_running:
            return format_html(
                '<button type="submit" class="button" style="background:#a61e1e;color:white;" '
                'formaction="{}" formmethod="post" '
                'onclick="return confirm(\'Остановить парсер для этого аккаунта?\');">Stop</button>',
                stop_url,
            )

        return format_html(
            '<button type="submit" class="button" formaction="{}" formmethod="post">Start</button>',
            run_url,
        )

    def run_parse_event_for_account_view(self, request, object_id):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])

        user = getattr(request, 'user', None)
        user_tag = f"user_id={getattr(user, 'id', None)} username={getattr(user, 'username', None)}"
        ip = request.META.get('REMOTE_ADDR')
        logger.info(
            "parser_start requested (%s, ip=%s) account_id=%s",
            user_tag,
            ip,
            object_id,
        )

        account = self.get_object(request, object_id)
        if account is None:
            logger.warning(
                "parser_start failed: account not found (%s, ip=%s) account_id=%s",
                user_tag,
                ip,
                object_id,
            )
            raise Http404('AccountVideoAnalytics not found')

        if account.is_parser_running:
            logger.warning(
                "parser_start skipped: already running (%s, ip=%s) account_id=%s",
                user_tag,
                ip,
                account.id,
            )
            self.message_user(
                request,
                f'Парсер уже запущен для аккаунта: {account.name}',
                level=messages.WARNING,
            )
            return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_accountvideoanalytics_changelist'))

        try:
            async_res = parse_event_task.delay(account.id)
        except Exception:
            logger.exception(
                "parser_start failed: celery enqueue error (%s, ip=%s) account_id=%s",
                user_tag,
                ip,
                account.id,
            )
            self.message_user(
                request,
                f'Не удалось запустить парсер для аккаунта: {account.name} — ошибка постановки задачи в Celery (см. логи)',
                level=messages.ERROR,
            )
            return redirect(
                request.META.get('HTTP_REFERER')
                or reverse('admin:agromash_accountvideoanalytics_changelist')
            )

        AccountVideoAnalytics.objects.filter(pk=account.id).update(
            parser_status=AccountVideoAnalytics.PARSER_STATUS_STARTING,
            parser_task_id=async_res.id,
            parser_stop_requested=False,
            parser_last_error=None,
        )

        logger.info(
            "parser_start enqueued ok (%s, ip=%s) account_id=%s task_id=%s",
            user_tag,
            ip,
            account.id,
            async_res.id,
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

        user = getattr(request, 'user', None)
        user_tag = f"user_id={getattr(user, 'id', None)} username={getattr(user, 'username', None)}"
        ip = request.META.get('REMOTE_ADDR')
        logger.info(
            "parser_stop requested (%s, ip=%s) account_id=%s",
            user_tag,
            ip,
            object_id,
        )

        account = self.get_object(request, object_id)
        if account is None:
            logger.warning(
                "parser_stop failed: account not found (%s, ip=%s) account_id=%s",
                user_tag,
                ip,
                object_id,
            )
            raise Http404('AccountVideoAnalytics not found')

        try:
            task_id = request_stop_parser(account_id=account.id, terminate=True)
        except Exception:
            logger.exception(
                "parser_stop failed: request_stop_parser exception (%s, ip=%s) account_id=%s",
                user_tag,
                ip,
                account.id,
            )
            self.message_user(
                request,
                f'Не удалось запросить остановку парсера для аккаунта: {account.name} (см. логи)',
                level=messages.ERROR,
            )
            return redirect(
                request.META.get('HTTP_REFERER')
                or reverse('admin:agromash_accountvideoanalytics_changelist')
            )

        logger.info(
            "parser_stop requested ok (%s, ip=%s) account_id=%s task_id=%s",
            user_tag,
            ip,
            account.id,
            task_id,
        )
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

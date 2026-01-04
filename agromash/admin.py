from django.contrib import admin, messages
from django import forms
import logging
import time
from django.contrib.auth import password_validation
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm as DjangoUserChangeForm
from django.contrib.auth.models import User
from django.utils.html import format_html
from django.utils import timezone
import datetime
from django.db.models import Count
from django.conf import settings
from django.http import Http404
from django.http import HttpResponseNotAllowed
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.urls import path, reverse
from .models import (
    AccountVideoAnalytics,
    Alarm,
    Monitor,
    ReportRunLog,
    TelegramReportSubscription,
    TelegramSubscriber,
    TelegramSubscriberMonitorSubscription,
)

from .tasks import parse_event_task, request_stop_parser
from .services.report_scheduler import compute_next_run_at
from .services.reporting import generate_report_attachments
from .services.telegram_client import send_document, send_message

# -----------------
# Django admin: название во вкладке браузера / заголовки
# -----------------
# В стандартных шаблонах Django admin текст во вкладке формируется на основе
# `admin.site.site_title`.
admin.site.site_header = "BTK Analitics"
admin.site.site_title = "BTK Analitics Admin"
admin.site.index_title = "Управление"


# -----------------
# Django admin: удобная установка пароля для Users (Authentication and Authorization)
# -----------------
class UserChangeFormWithPassword(DjangoUserChangeForm):
    """Добавляет поля для установки нового пароля прямо на странице редактирования User."""

    new_password1 = forms.CharField(
        label="Новый пароль",
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text="Если оставить пустым — пароль не изменится.",
    )
    new_password2 = forms.CharField(
        label="Повторите пароль",
        widget=forms.PasswordInput(render_value=False),
        required=False,
    )

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("new_password1")
        p2 = cleaned.get("new_password2")

        # Оба поля пустые => пароль не меняем.
        if not p1 and not p2:
            return cleaned

        if p1 != p2:
            raise forms.ValidationError("Пароли не совпадают")

        # Учитываем настройки валидаторов паролей Django.
        password_validation.validate_password(p1, self.instance)
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        p1 = self.cleaned_data.get("new_password1")
        if p1:
            user.set_password(p1)

        if commit:
            user.save()
            self.save_m2m()
        return user


class UserAdminWithPassword(DjangoUserAdmin):
    form = UserChangeFormWithPassword

    # Добавляем блок с полями пароля в форму редактирования.
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Смена пароля",
            {
                "fields": (
                    "new_password1",
                    "new_password2",
                )
            },
        ),
    )


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass
admin.site.register(User, UserAdminWithPassword)


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
    list_display = ('alarm_id', 'topic', 'monitor_name', 'start_time_human', 'end_time_human', 'snapshot_preview')
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
    )

    @staticmethod
    def _to_aware_dt(value: int):
        """BigInteger timestamp -> aware datetime.

        В данных VA встречаются epoch в секундах или миллисекундах.
        Эвристика: > 1e12 считаем миллисекундами.
        """
        if value is None:
            return None
        ts = int(value)
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)

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


class TelegramSubscriberMonitorSubscriptionInline(admin.TabularInline):
    model = TelegramSubscriberMonitorSubscription
    extra = 0
    autocomplete_fields = ('monitor',)


class TelegramSubscriberAdminForm(forms.ModelForm):
    """Форма для массового выбора мониторов одним действием.

    `TelegramSubscriber.subscribed_monitors` объявлен с `through=...`, поэтому
    стандартный виджет ManyToMany в админке не показывается. Даем отдельное
    поле с чекбоксами и синхронизируем связь через `.set()`.
    """

    subscribed_monitors = forms.ModelMultipleChoiceField(
        label="Подписанные мониторы",
        queryset=Monitor.objects.all().order_by("monitor_id"),
        required=False,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                # Нужен, чтобы повесить CSS и сделать подписи в одну строку.
                "class": "agromash-subscribed-monitors",
            }
        ),
        help_text="Отметьте мониторы, которые этот подписчик должен отслеживать.",
    )

    class Meta:
        model = TelegramSubscriber
        # Legacy JSON поле оставляем, но в админке не редактируем.
        fields = ("chat_id", "username", "subscribed_monitors")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["subscribed_monitors"].initial = self.instance.subscribed_monitors.all()

    def save(self, commit=True):
        instance: TelegramSubscriber = super().save(commit=commit)

        # Для M2M через through объект должен быть сохранен.
        if instance.pk:
            instance.subscribed_monitors.set(self.cleaned_data.get("subscribed_monitors"))
        return instance


@admin.register(TelegramSubscriber)
class TelegramSubscriberAdmin(admin.ModelAdmin):
    form = TelegramSubscriberAdminForm
    list_display = ('chat_id', 'username', 'subscribed_at', 'subscribed_monitors_count', 'subscribed_monitors_preview')
    search_fields = (
        'chat_id',
        'username',
        'subscribed_monitors__monitor_id',
        'subscribed_monitors__monitor_name',
    )
    list_filter = ('subscribed_at',)
    # Вместо inline-редактирования по одному используем массовый выбор в форме.
    inlines = ()
    readonly_fields = ("subscribed_at",)

    class Media:
        css = {
            "all": (
                "agromash/admin.css",
            )
        }

    def subscribed_monitors_count(self, obj: TelegramSubscriber):
        return obj.subscribed_monitors.count()

    subscribed_monitors_count.short_description = 'Мониторов'

    def subscribed_monitors_preview(self, obj: TelegramSubscriber):
        qs = obj.subscribed_monitors.all().order_by('monitor_id')
        items = [f"{m.monitor_name} ({m.monitor_id})" for m in qs[:10]]
        suffix = "" if qs.count() <= 10 else " …"
        return ", ".join(items) + suffix

    subscribed_monitors_preview.short_description = 'Подписанные мониторы'


@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    list_display = ('monitor_id', 'monitor_name', 'topic', 'subscribers_count', 'created_at', 'updated_at')
    search_fields = (
        'monitor_id',
        'monitor_name',
        'topic',
        'subscribers__chat_id',
        'subscribers__username',
    )
    list_filter = ('topic',)
    readonly_fields = ('created_at', 'updated_at')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(_subscribers_count=Count('subscribers', distinct=True))

    def subscribers_count(self, obj: Monitor):
        return getattr(obj, '_subscribers_count', 0)

    subscribers_count.short_description = 'Подписчиков'
    subscribers_count.admin_order_field = '_subscribers_count'


@admin.register(TelegramReportSubscription)
class TelegramReportSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "subscriber",
        "frequency",
        "period_from_minutes",
        "period_to_minutes",
        "send_pdf",
        "send_xlsx",
        "enabled",
        "last_sent_at",
        "next_run_at",
    )
    list_filter = (
        "enabled",
        "frequency",
        "send_pdf",
        "send_xlsx",
    )
    search_fields = (
        "subscriber__chat_id",
        "subscriber__username",
        "monitors__monitor_id",
        "monitors__monitor_name",
    )
    filter_horizontal = ("monitors",)
    autocomplete_fields = ("subscriber",)

    def get_list_display(self, request):
        base = super().get_list_display(request)

        def send_now_controls(obj: TelegramReportSubscription):
            return self._send_now_controls(request, obj)

        send_now_controls.short_description = "Отчёт"
        return (*base, send_now_controls)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<path:object_id>/send-now/',
                self.admin_site.admin_view(self.send_now_view),
                name='agromash_telegramreportsubscription_send_now',
            ),
        ]
        return custom_urls + urls

    def _send_now_controls(self, request, obj: TelegramReportSubscription):
        csrf = get_token(request)
        send_now_url = reverse('admin:agromash_telegramreportsubscription_send_now', args=[obj.pk])

        disabled = "" if (obj.subscriber_id and getattr(obj.subscriber, 'chat_id', None)) else "disabled"
        title = "" if not disabled else "Нет chat_id у подписчика"

        return format_html(
            '<form method="post" action="{}" style="display:inline">'
            '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
            '<button type="submit" class="button" {} title="{}">Сформировать и отправить</button>'
            '</form>',
            send_now_url,
            csrf,
            disabled,
            title,
        )

    def send_now_view(self, request, object_id):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])

        sub: TelegramReportSubscription = self.get_object(request, object_id)
        if sub is None:
            raise Http404('TelegramReportSubscription not found')

        if not sub.subscriber_id or not getattr(sub.subscriber, 'chat_id', None):
            self.message_user(request, 'У подписчика не задан chat_id — отправка невозможна', level=messages.ERROR)
            return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_telegramreportsubscription_changelist'))

        now = timezone.now()
        t0 = time.monotonic()
        run_log = ReportRunLog.objects.create(subscription=sub, subscriber=sub.subscriber, started_at=now)

        try:
            caption, attachments, rows_count = generate_report_attachments(sub=sub, now=now)
            if not attachments:
                send_message(chat_id=sub.subscriber.chat_id, text=caption, meta={"source": "admin", "subscription_id": sub.id})
            else:
                for idx, (filename, content, mime_type) in enumerate(attachments):
                    send_document(
                        chat_id=sub.subscriber.chat_id,
                        filename=filename,
                        content=content,
                        mime_type=mime_type,
                        caption=caption if idx == 0 else None,
                        meta={"source": "admin", "subscription_id": sub.id},
                    )

            sub.last_sent_at = now
            sub.next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
            sub.save(update_fields=["last_sent_at", "next_run_at", "updated_at"])

            run_log.finished_at = timezone.now()
            run_log.duration_ms = int((time.monotonic() - t0) * 1000)
            run_log.ok = True
            run_log.error = ""
            run_log.alarms_count = int(rows_count or 0)
            run_log.attachments_count = len(attachments or [])
            run_log.save(
                update_fields=[
                    "finished_at",
                    "duration_ms",
                    "ok",
                    "error",
                    "alarms_count",
                    "attachments_count",
                ]
            )

            self.message_user(
                request,
                f"Отчёт отправлен (subscription_id={sub.id}, rows={rows_count}, files={len(attachments)})",
                level=messages.SUCCESS,
            )
        except Exception:
            run_log.finished_at = timezone.now()
            run_log.duration_ms = int((time.monotonic() - t0) * 1000)
            run_log.ok = False
            run_log.error = "exception"
            run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])

            logger = logging.getLogger(__name__)
            logger.exception("Ошибка ручной отправки отчёта (subscription_id=%s)", sub.id)
            self.message_user(request, "Ошибка отправки отчёта — см. логи", level=messages.ERROR)

        return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_telegramreportsubscription_changelist'))

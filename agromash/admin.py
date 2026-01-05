from django.contrib import admin, messages
from django import forms
import logging
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
from django.shortcuts import redirect
from django.urls import path, reverse
from django.template.response import TemplateResponse
from .models import (
    AccountVideoAnalytics,
    Alarm,
    FuelOperation,
    FuelReport,
    Monitor,
    PlateIdentity,
    TelegramReportSubscription,
    TelegramSubscriber,
    TelegramSubscriberMonitorSubscription,
)

from .tasks import parse_event_task, request_stop_parser
from .tasks import send_report_now, send_report_range_now, send_email_report_now

from agromash.services.fuel_report_importer import FuelImportError, import_fuel_report_from_xlsx


logger = logging.getLogger(__name__)


class FuelReportImportForm(forms.Form):
    xlsx_file = forms.FileField(label="XLSX файл", required=True)
    period_start = forms.DateField(label="Период с", required=False)
    period_end = forms.DateField(label="Период по", required=False)

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
        run_url = reverse('admin:agromash_accountvideoanalytics_run_parse_event_for_account', args=[obj.pk])
        stop_url = reverse('admin:agromash_accountvideoanalytics_stop_parse_event_for_account', args=[obj.pk])

        # ВАЖНО: в changelist Django admin уже есть внешний <form id="changelist-form"> с CSRF.
        # Вложенные <form> внутри таблицы — невалидный HTML и может ломать submit (особенно на последней строке).
        # Поэтому используем HTML5 `formaction`/`formmethod` без вложенных форм.
        if obj.is_parser_running:
            return format_html(
                '<button type="submit" class="button" style="background:#a61e1e;color:white;" '
                'formaction="{}" formmethod="post">Stop</button>',
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
        "email",
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

        def send_range_controls(obj: TelegramReportSubscription):
            return self._send_range_controls(request, obj)

        def send_email_now_controls(obj: TelegramReportSubscription):
            return self._send_email_now_controls(request, obj)

        send_now_controls.short_description = "Отчёт"
        send_email_now_controls.short_description = "Email"
        send_range_controls.short_description = "Диапазон"
        return (*base, send_now_controls, send_email_now_controls, send_range_controls)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<path:object_id>/send-now/',
                self.admin_site.admin_view(self.send_now_view),
                name='agromash_telegramreportsubscription_send_now',
            ),
            path(
                '<path:object_id>/send-range/',
                self.admin_site.admin_view(self.send_range_view),
                name='agromash_telegramreportsubscription_send_range',
            ),
            path(
                '<path:object_id>/send-email-now/',
                self.admin_site.admin_view(self.send_email_now_view),
                name='agromash_telegramreportsubscription_send_email_now',
            ),
        ]
        return custom_urls + urls

    def _send_now_controls(self, request, obj: TelegramReportSubscription):
        send_now_url = reverse('admin:agromash_telegramreportsubscription_send_now', args=[obj.pk])

        disabled = "" if (obj.subscriber_id and getattr(obj.subscriber, 'chat_id', None)) else "disabled"
        title = "" if not disabled else "Нет chat_id у подписчика"

        # Аналогично parser buttons: используем formaction без вложенных <form>.
        return format_html(
            '<button type="submit" class="button" formaction="{}" formmethod="post" {} title="{}">'
            'Сформировать и отправить'
            '</button>',
            send_now_url,
            disabled,
            title,
        )

    def _send_range_controls(self, request, obj: TelegramReportSubscription):
        send_range_url = reverse('admin:agromash_telegramreportsubscription_send_range', args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">Выбрать период…</a>',
            send_range_url,
        )

    def _send_email_now_controls(self, request, obj: TelegramReportSubscription):
        send_url = reverse('admin:agromash_telegramreportsubscription_send_email_now', args=[obj.pk])
        disabled = "" if (obj.email and obj.enabled) else "disabled"
        title = "" if not disabled else "Нет email или подписка выключена"
        return format_html(
            '<button type="submit" class="button" formaction="{}" formmethod="post" {} title="{}">'
            'Отправить email'
            '</button>',
            send_url,
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

        try:
            async_res = send_report_now.delay(sub.id, source="admin")
            self.message_user(
                request,
                f"Отправка отчёта поставлена в очередь Celery (subscription_id={sub.id}, task_id={async_res.id})",
                level=messages.SUCCESS,
            )
        except Exception:
            logging.getLogger(__name__).exception("Ошибка постановки отчёта в очередь (subscription_id=%s)", sub.id)
            self.message_user(request, "Не удалось поставить задачу в очередь Celery — см. логи", level=messages.ERROR)

        return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_telegramreportsubscription_changelist'))

    def send_email_now_view(self, request, object_id):
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])

        sub: TelegramReportSubscription = self.get_object(request, object_id)
        if sub is None:
            raise Http404('TelegramReportSubscription not found')

        if not sub.email:
            self.message_user(request, 'У подписки не задан email — отправка невозможна', level=messages.ERROR)
            return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_telegramreportsubscription_changelist'))

        if not sub.enabled:
            self.message_user(request, 'Подписка выключена — отправка невозможна', level=messages.ERROR)
            return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_telegramreportsubscription_changelist'))

        try:
            async_res = send_email_report_now.delay(sub.id, source="admin")
            self.message_user(
                request,
                f"Email-отправка отчёта поставлена в очередь Celery (subscription_id={sub.id}, task_id={async_res.id})",
                level=messages.SUCCESS,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Ошибка постановки email-отчёта в очередь (subscription_id=%s)",
                sub.id,
            )
            self.message_user(request, "Не удалось поставить email-задачу в очередь Celery — см. логи", level=messages.ERROR)

        return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_telegramreportsubscription_changelist'))


class _ReportRangeForm(forms.Form):
    start = forms.DateTimeField(
        label="Начало периода",
        required=True,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    end = forms.DateTimeField(
        label="Конец периода",
        required=True,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )


def _dt_to_local_input(dt: timezone.datetime) -> str:
    dt_local = timezone.localtime(dt)
    return dt_local.strftime("%Y-%m-%dT%H:%M")


def _get_default_range_initial() -> dict:
    now = timezone.now()
    start = now - timezone.timedelta(hours=24)
    return {
        "start": _dt_to_local_input(start),
        "end": _dt_to_local_input(now),
    }


def _render_admin_form(request, *, title: str, form: forms.Form, object_id: str):
    context = admin.site.each_context(request)
    context.update(
        {
            "title": title,
            "form": form,
            "object_id": object_id,
        }
    )
    return TemplateResponse(request, "admin/agromash/telegramreportsubscription/send_range.html", context)


def _redirect_back(request):
    return redirect(request.META.get('HTTP_REFERER') or reverse('admin:agromash_telegramreportsubscription_changelist'))


def _subscription_for_admin(admin_obj: TelegramReportSubscriptionAdmin, request, object_id):
    sub = admin_obj.get_object(request, object_id)
    if sub is None:
        raise Http404('TelegramReportSubscription not found')
    return sub


def _enqueue_range_task(sub: TelegramReportSubscription, start_dt, end_dt):
    # В Celery отправляем ISO; dt -> локальная ISO (без TZ) тоже норм,
    # в задаче будет make_aware.
    return send_report_range_now.delay(sub.id, start_dt.isoformat(), end_dt.isoformat(), source="admin")


def send_range_view(self: TelegramReportSubscriptionAdmin, request, object_id):
    """Показать форму выбора диапазона и поставить задачу отчёта в очередь."""

    sub = _subscription_for_admin(self, request, object_id)
    if not sub.subscriber_id or not getattr(sub.subscriber, 'chat_id', None):
        self.message_user(request, 'У подписчика не задан chat_id — отправка невозможна', level=messages.ERROR)
        return _redirect_back(request)

    if request.method == "GET":
        form = _ReportRangeForm(initial=_get_default_range_initial())
        return _render_admin_form(request, title=f"Отчёт по диапазону (id={sub.id})", form=form, object_id=object_id)

    if request.method != "POST":
        return HttpResponseNotAllowed(['GET', 'POST'])

    form = _ReportRangeForm(request.POST)
    if not form.is_valid():
        return _render_admin_form(request, title=f"Отчёт по диапазону (id={sub.id})", form=form, object_id=object_id)

    start_dt = form.cleaned_data["start"]
    end_dt = form.cleaned_data["end"]
    try:
        async_res = _enqueue_range_task(sub, start_dt, end_dt)
        self.message_user(
            request,
            f"Отчёт по диапазону поставлен в очередь Celery (subscription_id={sub.id}, task_id={async_res.id})",
            level=messages.SUCCESS,
        )
    except Exception:
        logging.getLogger(__name__).exception("Ошибка постановки range-отчёта в очередь (subscription_id=%s)", sub.id)
        self.message_user(request, "Не удалось поставить задачу в очередь Celery — см. логи", level=messages.ERROR)

    return _redirect_back(request)


# bind method to admin class (чтобы не раздувать класс ниже)
TelegramReportSubscriptionAdmin.send_range_view = send_range_view


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
    readonly_fields = ("created_at", "rows_count", "imported_ok", "import_error", "source_sha256")

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "import-xlsx/",
                self.admin_site.admin_view(self.import_xlsx_view),
                name="agromash_fuelreport_import_xlsx",
            ),
        ]
        return custom + urls

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


@admin.register(FuelOperation)
class FuelOperationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "report",
        "card_number",
        "operation_at",
        "product_name",
        "quantity",
        "unit",
        "total_cost",
        "station_owner",
        "station_number",
    )
    list_filter = ("station_owner", "product_name")
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

import logging

from django.contrib import admin, messages
from django import forms
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from ..models import (
    Monitor,
    TelegramReportSubscription,
    TelegramSubscriber,
    TelegramSubscriberMonitorSubscription,
)
from ..tasks import send_email_report_now, send_report_now, send_report_range_now


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
        fields = ("chat_id", "username", "email", "subscribed_monitors")

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
    list_display = (
        'chat_id',
        'username',
        'email',
        'subscribed_at',
        'subscribed_monitors_count',
        'subscribed_monitors_preview',
    )
    search_fields = (
        'chat_id',
        'username',
        'email',
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

import logging

from django.conf import settings
from django.db import models, transaction
from django.contrib.auth.models import User
from django.utils import timezone

from agromash.services.telegram_client import send_message


logger = logging.getLogger(__name__)


def _va_status_icon(status: str) -> str:
    # Не ссылаемся на `AccountVideoAnalytics.*` здесь, чтобы избежать проблем
    # с порядком объявления (модуль импортируется целиком).
    return {
        "running": "🟢",
        "starting": "🟡",
        "stopping": "🟡",
        "stopped": "⚪️",
        "error": "🔴",
    }.get(str(status or ""), "🔔")


def _notify_va_parser_status_change(
    *,
    account_id: int,
    name: str,
    organization: str,
    old_status: str,
    new_status: str,
    source: str,
) -> None:
    """Best-effort: уведомить админов в Telegram о смене parser_status."""

    raw = getattr(settings, "TLG_CHAT_ID_ADMINS", None) or []
    chat_ids: list[int] = []
    for x in raw:
        try:
            chat_ids.append(int(str(x).strip()))
        except Exception:
            continue
    if not chat_ids:
        return

    text = (
        f"{_va_status_icon(new_status)} <b>VA parser status changed</b>\n"
        f"Account: <code>#{account_id}</code> {name} | {organization}\n"
        f"Status: <b>{old_status}</b> → <b>{new_status}</b>\n"
        f"At: {timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    meta = {
        "source": source,
        "model": "AccountVideoAnalytics",
        "account_id": account_id,
        "old_status": old_status,
        "new_status": new_status,
    }

    for cid in chat_ids:
        try:
            send_message(
                chat_id=int(cid),
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                meta=meta,
            )
        except Exception:
            logger.exception(
                "Failed to notify admins about parser_status change (account_id=%s, chat_id=%s)",
                account_id,
                cid,
            )


class AccountVideoAnalyticsQuerySet(models.QuerySet):
    def update(self, **kwargs):
        """Перехватываем `QuerySet.update(parser_status=...)` для оповещений.

        Django `.update()` не вызывает `.save()` и не шлёт сигналы, поэтому уведомление
        делаем здесь (best-effort).
        """

        if "parser_status" not in kwargs:
            return super().update(**kwargs)

        new_status = kwargs.get("parser_status")
        # если это не простое значение (F/Func) — не можем корректно сравнить
        if not isinstance(new_status, str):
            return super().update(**kwargs)

        # best-effort: собираем текущие значения до апдейта
        rows = list(self.values_list("id", "name", "organization", "parser_status"))
        updated = super().update(**kwargs)
        if not updated or not rows:
            return updated

        def _notify() -> None:
            for (acc_id, name, org, old_status) in rows:
                if old_status == new_status:
                    continue
                _notify_va_parser_status_change(
                    account_id=int(acc_id),
                    name=str(name or ""),
                    organization=str(org or ""),
                    old_status=str(old_status or ""),
                    new_status=str(new_status or ""),
                    source="queryset_update",
                )

        try:
            transaction.on_commit(_notify)
        except Exception:
            _notify()

        return updated


class AccountVideoAnalytics(models.Model):
    name = models.CharField(max_length=255, help_text="Account name")
    password = models.CharField(max_length=255, help_text="Account password")
    contract = models.CharField(max_length=255, help_text="Contract number")
    organization = models.CharField(max_length=255, help_text="Organization name")
    access_token = models.TextField(null=True, blank=True)
    refresh_token = models.TextField(null=True, blank=True)

    PARSER_STATUS_STOPPED = "stopped"
    PARSER_STATUS_STARTING = "starting"
    PARSER_STATUS_RUNNING = "running"
    PARSER_STATUS_STOPPING = "stopping"
    PARSER_STATUS_ERROR = "error"
    PARSER_STATUS_CHOICES = (
        (PARSER_STATUS_STOPPED, "Stopped"),
        (PARSER_STATUS_STARTING, "Starting"),
        (PARSER_STATUS_RUNNING, "Running"),
        (PARSER_STATUS_STOPPING, "Stopping"),
        (PARSER_STATUS_ERROR, "Error"),
    )

    parser_status = models.CharField(
        max_length=16,
        choices=PARSER_STATUS_CHOICES,
        default=PARSER_STATUS_STOPPED,
        db_index=True,
    )
    parser_task_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    parser_stop_requested = models.BooleanField(default=False)
    parser_started_at = models.DateTimeField(null=True, blank=True)
    parser_stopped_at = models.DateTimeField(null=True, blank=True)
    parser_heartbeat_at = models.DateTimeField(null=True, blank=True)
    parser_last_error = models.TextField(null=True, blank=True)

    objects = AccountVideoAnalyticsQuerySet.as_manager()

    def __str__(self):
        return f"{self.name} - {self.organization}"

    def save(self, *args, **kwargs):
        """Переопределение save(): оповещаем админов в Telegram при смене parser_status.

        Важно:
        - Срабатывает только для изменений, проходящих через `.save()`.
          Обновления через `QuerySet.update(...)` Django сигналов/`save()` не вызывают.
        - Отправка делается best-effort и не должна ломать сохранение.
        """

        old_status = None
        if self.pk:
            try:
                old_status = (
                    AccountVideoAnalytics.objects.filter(pk=self.pk)
                    .values_list("parser_status", flat=True)
                    .first()
                )
            except Exception:
                old_status = None

        super().save(*args, **kwargs)

        new_status = getattr(self, "parser_status", None)
        if old_status is None or old_status == new_status:
            return

        def _notify() -> None:
            _notify_va_parser_status_change(
                account_id=int(self.pk or 0),
                name=str(getattr(self, "name", "") or ""),
                organization=str(getattr(self, "organization", "") or ""),
                old_status=str(old_status or ""),
                new_status=str(new_status or ""),
                source="model_save",
            )

        # Отправляем после коммита транзакции (если мы внутри atomic).
        try:
            transaction.on_commit(_notify)
        except Exception:
            # best-effort fallback
            try:
                _notify()
            except Exception:
                logger.exception("Failed to run parser_status change notification")

    @property
    def is_parser_running(self) -> bool:
        """Оптимистичная индикация состояния для админки.

        Celery воркер не всегда предоставляет result backend, поэтому ориентируемся на:
          - parser_status == running
          - heartbeat не старше 2 минут (если heartbeat есть)
        
        Важно: если heartbeat устарел, но статус всё ещё running — это признак
        рассинхронизации (задача убита, но статус не обновлён). Периодическая задача
        `agromash.check_parser_heartbeats` должна корректировать такие случаи.
        """
        if self.parser_status != self.PARSER_STATUS_RUNNING:
            return False
        if not self.parser_heartbeat_at:
            # Нет heartbeat — считаем, что парсер только стартовал
            return True
        
        threshold = timezone.now() - timezone.timedelta(minutes=2)
        is_fresh = self.parser_heartbeat_at >= threshold
        
        if not is_fresh:
            # Heartbeat устарел — логируем для диагностики
            age_sec = (timezone.now() - self.parser_heartbeat_at).total_seconds()
            logger.warning(
                "is_parser_running: stale heartbeat detected (account_id=%s, age=%.1fs, status=%s)",
                self.pk,
                age_sec,
                self.parser_status,
            )
        
        return is_fresh


class Alarm(models.Model):
    monitor_id = models.IntegerField()
    monitor_name = models.CharField(max_length=255)
    # Нормализованная связь с Monitor (best-effort). Дублирует monitor_id/monitor_name,
    # потому что исторически Alarm сохранялся без FK.
    monitor_ref = models.ForeignKey(
        'Monitor',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='alarms',
    )
    alarm_id = models.CharField(max_length=255, unique=True)
    topic = models.CharField(max_length=255)
    start_time = models.BigIntegerField()
    end_time = models.BigIntegerField()
    event_id = models.BigIntegerField()
    original_quality_snapshot = models.TextField(null=True, blank=True)
    plate_identities = models.JSONField(null=True, blank=True)
    face_identities = models.JSONField(null=True, blank=True)
    snapshots = models.JSONField(null=True, blank=True)
    data = models.JSONField()
    account = models.ForeignKey(AccountVideoAnalytics, on_delete=models.CASCADE)

    def __str__(self):
        return f"Alarm {self.alarm_id} - {self.topic}"

    @property
    def monitor_name_second_token(self) -> str:
        """Вычисляемое поле: второй элемент из `monitor_name`, разделённого пробелами.

        Пример:
          - monitor_name="КПП 12 Въезд" -> "12"

        Если `monitor_name` пустой или состоит из одного слова — вернёт пустую строку.
        """
        raw = str(self.monitor_name or "").strip()
        if not raw:
            return ""

        parts = raw.split()
        return parts[1] if len(parts) >= 2 else ""



class Monitor(models.Model):
    monitor_id = models.CharField(max_length=255, unique=True)
    monitor_name = models.CharField(max_length=255)
    topic = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.monitor_name} (ID: {self.monitor_id})"


class UserMonitorAccess(models.Model):
    """Права доступа Django User к Monitor (для web-страницы событий).

    Аналогично [`TelegramSubscriberMonitorSubscription`](agromash/models.py:132),
    но для пользователей Django (вход по сессии).
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='monitor_accesses')
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name='user_accesses')
    enabled = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name='Активно',
        help_text='Если выключено — монитор не доступен пользователю в web-интерфейсе.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (('user', 'monitor'),)
        verbose_name = 'Доступ пользователя к монитору'
        verbose_name_plural = 'Доступ пользователей к мониторам'
        permissions = (
            ('can_view_events', 'Can view events dashboard'),
        )

    def __str__(self):
        return f"user_id={self.user_id} -> monitor_id={self.monitor_id}"


class AlarmCase(models.Model):
    """Дополнительная карточка к Alarm: описание/примечание + аудит."""

    alarm = models.OneToOneField(
        Alarm,
        on_delete=models.CASCADE,
        related_name='case',
    )
    description = models.TextField(blank=True, verbose_name='Описание')
    note = models.TextField(blank=True, verbose_name='Примечание')

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='alarm_cases_created',
    )
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='alarm_cases_updated',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Карточка события'
        verbose_name_plural = 'Карточки событий'

    def __str__(self):
        return f"AlarmCase(alarm_id={getattr(self.alarm, 'alarm_id', self.alarm_id)})"


def _alarm_document_upload_to(instance: 'AlarmDocument', filename: str) -> str:
    # Не используем id Alarm напрямую в пути (он может быть None при создании),
    # но в большинстве случаев case/alarm уже существуют.
    alarm_id = ""
    try:
        alarm_id = str(getattr(instance.case.alarm, 'alarm_id', '') or '')
    except Exception:
        alarm_id = ""
    alarm_id = alarm_id.replace('/', '_')[:64]
    return f"alarm_docs/{alarm_id or 'unknown'}/{timezone.now().strftime('%Y/%m/%d')}/{filename}"


class AlarmDocument(models.Model):
    """Документ/изображение, прикреплённое к AlarmCase."""

    case = models.ForeignKey(AlarmCase, on_delete=models.CASCADE, related_name='documents')
    file = models.FileField(upload_to=_alarm_document_upload_to)
    title = models.CharField(max_length=255, blank=True, verbose_name='Название')
    uploaded_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='alarm_documents_uploaded',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Документ события'
        verbose_name_plural = 'Документы событий'
        ordering = ('-uploaded_at',)

    def __str__(self):
        return f"AlarmDocument(case_id={self.case_id}, file={getattr(self.file, 'name', '')})"
    
    
class TelegramSubscriber(models.Model):
    chat_id = models.BigIntegerField(unique=True)

    # Legacy storage (до перехода на ManyToMany). Оставлено для обратной совместимости/миграции.
    subscribed_monitor_ids = models.JSONField(default=list, blank=True)

    subscribed_monitors = models.ManyToManyField(
        'Monitor',
        related_name='subscribers',
        blank=True,
        verbose_name='Подписанные мониторы',
        through='TelegramSubscriberMonitorSubscription',
    )
    username = models.CharField(max_length=255, blank=True, null=True)
    email = models.EmailField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name="Email",
        help_text="Если задан — может использоваться для ручной отправки отчётов.",
    )
    subscribed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Subscriber {self.chat_id} - {self.username or 'Unknown'}"


class TelegramSubscriberMonitorSubscription(models.Model):
    """Связь many-to-many между TelegramSubscriber и Monitor."""

    subscriber = models.ForeignKey(TelegramSubscriber, on_delete=models.CASCADE)
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE)
    enabled = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name="Активно",
        help_text="Если выключено — оповещения по Alarm для этого монитора подписчику не отправляются.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'subscribed_monitors'
        unique_together = (
            ('subscriber', 'monitor'),
        )
        verbose_name = 'Подписка на монитор'
        verbose_name_plural = 'Подписки на мониторы'

    def __str__(self):
        return f"{self.subscriber_id} -> {self.monitor_id}"


class TelegramReportSubscription(models.Model):
    """Настройки периодической отправки отчётов подписчику в Telegram."""

    FREQ_EVERY_10_MIN = "10m"
    FREQ_HOURLY = "hourly"
    FREQ_DAILY = "daily"
    FREQ_WEEKLY = "weekly"
    FREQ_MONTHLY = "monthly"

    FREQUENCY_CHOICES = (
        (FREQ_EVERY_10_MIN, "Каждые 10 минут"),
        (FREQ_HOURLY, "Каждый час"),
        (FREQ_DAILY, "Каждый день"),
        (FREQ_WEEKLY, "Каждую неделю"),
        (FREQ_MONTHLY, "Каждый месяц"),
    )

    subscriber = models.ForeignKey(
        TelegramSubscriber,
        on_delete=models.CASCADE,
        related_name="report_subscriptions",
        verbose_name="Подписчик",
    )

    email = models.EmailField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name="Email получателя",
        help_text="Если задан — отчёт будет отправляться также на email.",
    )
    monitors = models.ManyToManyField(
        Monitor,
        blank=True,
        related_name="report_subscriptions",
        verbose_name="Мониторы",
        help_text="Если не выбрать мониторы — отчёт будет по всем мониторам.",
    )

    # Период относительно текущего времени на момент отправки.
    # Пример: from=60, to=0 => последние 60 минут.
    period_from_minutes = models.PositiveIntegerField(
        default=60,
        verbose_name="Период ОТ (минут назад)",
    )
    period_to_minutes = models.PositiveIntegerField(
        default=0,
        verbose_name="Период ДО (минут назад)",
        help_text="0 означает 'по текущее время'.",
    )

    frequency = models.CharField(
        max_length=16,
        choices=FREQUENCY_CHOICES,
        default=FREQ_HOURLY,
        verbose_name="Частота отправки",
    )

    send_pdf = models.BooleanField(default=True, verbose_name="Отправлять PDF")
    send_xlsx = models.BooleanField(default=True, verbose_name="Отправлять Excel (XLSX)")

    enabled = models.BooleanField(default=True, verbose_name="Включено")
    last_sent_at = models.DateTimeField(null=True, blank=True, verbose_name="Последняя отправка")
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Следующая отправка")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Report subscription"
        verbose_name_plural = "Report subscriptions"

    def __str__(self):
        return f"ReportSubscription(subscriber={self.subscriber_id}, freq={self.frequency})"


class TelegramEventLog(models.Model):
    """Журнал отправленных Telegram-оповещений (для dashboard/диагностики)."""

    KIND_MESSAGE = "message"
    KIND_PHOTO = "photo"
    KIND_DOCUMENT = "document"

    KIND_CHOICES = (
        (KIND_MESSAGE, "Message"),
        (KIND_PHOTO, "Photo"),
        (KIND_DOCUMENT, "Document"),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    subscriber = models.ForeignKey(
        TelegramSubscriber,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="telegram_event_logs",
    )
    chat_id = models.BigIntegerField(db_index=True)

    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_MESSAGE, db_index=True)
    ok = models.BooleanField(default=True, db_index=True)
    status_code = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True)

    # Полезно для UI: что именно отправляли
    text = models.TextField(blank=True)
    filename = models.CharField(max_length=255, blank=True)

    # Связь с Alarm, если это оповещение по тревоге
    alarm = models.ForeignKey(
        Alarm,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="telegram_event_logs",
    )

    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Telegram log"
        verbose_name_plural = "Telegram logs"
        ordering = ("-created_at",)

    def __str__(self):
        return f"TelegramEventLog(chat_id={self.chat_id}, ok={self.ok}, kind={self.kind})"


class ReportRunLog(models.Model):
    """Журнал формирований/отправок отчётов (с временем генерации)."""

    CHANNEL_TELEGRAM = "telegram"
    CHANNEL_EMAIL = "email"
    CHANNEL_CHOICES = (
        (CHANNEL_TELEGRAM, "Telegram"),
        (CHANNEL_EMAIL, "Email"),
    )

    channel = models.CharField(
        max_length=16,
        choices=CHANNEL_CHOICES,
        default=CHANNEL_TELEGRAM,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    subscription = models.ForeignKey(
        TelegramReportSubscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="run_logs",
    )
    subscriber = models.ForeignKey(
        TelegramSubscriber,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="report_run_logs",
    )

    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    ok = models.BooleanField(default=True, db_index=True)
    error = models.TextField(blank=True)

    alarms_count = models.PositiveIntegerField(default=0)
    attachments_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Report run log"
        verbose_name_plural = "Report run logs"
        ordering = ("-created_at",)

    def __str__(self):
        return f"ReportRunLog(subscription={self.subscription_id}, ok={self.ok})"


class FuelReport(models.Model):
    """Импортированный пооперационный отчёт по топливным картам (XLSX)."""

    created_at = models.DateTimeField(auto_now_add=True)

    title = models.CharField(max_length=255, blank=True)
    contract_number = models.CharField(max_length=255, blank=True, db_index=True)
    organization_name = models.CharField(max_length=500, blank=True)

    period_start = models.DateField(null=True, blank=True, db_index=True)
    period_end = models.DateField(null=True, blank=True, db_index=True)

    source_filename = models.CharField(max_length=255, blank=True)
    source_sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    imported_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='fuel_reports',
    )

    rows_count = models.PositiveIntegerField(default=0)
    imported_ok = models.BooleanField(default=True, db_index=True)
    import_error = models.TextField(blank=True)

    # --- Экспорт (кэш XLSX, чтобы не блокировать админку долгой генерацией) ---
    EXPORT_STATUS_NONE = "none"
    EXPORT_STATUS_PENDING = "pending"
    EXPORT_STATUS_READY = "ready"
    EXPORT_STATUS_ERROR = "error"
    EXPORT_STATUS_CHOICES = (
        (EXPORT_STATUS_NONE, "Not generated"),
        (EXPORT_STATUS_PENDING, "Generating"),
        (EXPORT_STATUS_READY, "Ready"),
        (EXPORT_STATUS_ERROR, "Error"),
    )

    export_xlsx_status = models.CharField(
        max_length=16,
        choices=EXPORT_STATUS_CHOICES,
        default=EXPORT_STATUS_NONE,
        db_index=True,
    )
    export_xlsx_task_id = models.CharField(max_length=255, blank=True)
    export_xlsx_generated_at = models.DateTimeField(null=True, blank=True)
    export_xlsx_error = models.TextField(blank=True)
    # bytes готового файла (в БД, чтобы не требовать MEDIA_ROOT)
    export_xlsx_content = models.BinaryField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self) -> str:
        parts = ["FuelReport"]
        if self.contract_number:
            parts.append(f"contract={self.contract_number}")
        if self.period_start and self.period_end:
            parts.append(f"{self.period_start}..{self.period_end}")
        return " ".join(parts)


class FuelOperation(models.Model):
    """Строка операции из пооперационного отчёта."""

    report = models.ForeignKey(FuelReport, on_delete=models.CASCADE, related_name='operations')

    card_number = models.CharField(max_length=32, db_index=True)
    department_number = models.CharField(max_length=64, blank=True, db_index=True)

    operation_at = models.DateTimeField(db_index=True)

    product_name = models.CharField(max_length=255, blank=True)
    product_code = models.CharField(max_length=64, blank=True, db_index=True)

    quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=32, blank=True)

    unit_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    vat = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    discount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    service_percent = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    service_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    total_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_vat = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    station_owner = models.CharField(max_length=255, blank=True)
    station_number = models.CharField(max_length=64, blank=True)
    pump_section = models.CharField(max_length=64, blank=True)

    driver_name = models.CharField(max_length=255, blank=True)
    vehicle_number = models.CharField(max_length=64, blank=True)

    # --- Анализ (обогащение) ---
    plate_identity = models.ForeignKey(
        'PlateIdentity',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='fuel_operations',
        help_text="Связанный PlateIdentity, подобранный по card_number -> owner_middle_name",
    )
    matched_alarms = models.JSONField(
        default=list,
        blank=True,
        help_text="Список совпавших Alarm (в пределах окна времени). Формат: [{id, alarm_id, start_time, start_time_iso, delta_seconds}]",
    )
    matched_alarm_snapshot_urls = models.JSONField(
        default=list,
        blank=True,
        help_text="Список URL/путей на снимки Alarm.original_quality_snapshot для matched_alarms (best-effort).",
    )
    fallback_plate_numbers = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Если PlateIdentity не подобран по card_number -> owner_middle_name — "
            "список уникальных объектов plates (из Alarm.plate_identities) для matched_alarms."
        ),
    )
    analyzed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ('-operation_at',)
        indexes = [
            models.Index(fields=['card_number', 'operation_at']),
        ]

    def __str__(self) -> str:
        return f"FuelOperation(card={self.card_number}, at={self.operation_at})"


class PlateIdentity(models.Model):
    """Нормализованное хранилище распознанных номеров (topic=PlateMatched).

    Источник: `Alarm.plate_identities`.
    Поле `number` уникально по всей БД.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    number = models.CharField(max_length=32, unique=True, db_index=True)
    state = models.CharField(max_length=8, blank=True)
    plate_external_id = models.BigIntegerField(null=True, blank=True)

    owner_last_name = models.CharField(max_length=255, blank=True)
    owner_first_name = models.CharField(max_length=255, blank=True)
    owner_middle_name = models.CharField(max_length=255, blank=True)

    list_external_id = models.BigIntegerField(null=True, blank=True)
    list_name = models.CharField(max_length=255, blank=True)
    list_level = models.IntegerField(null=True, blank=True)

    last_alarm = models.ForeignKey(
        Alarm,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='plate_identity_last_seen',
    )

    class Meta:
        ordering = ("number",)

    def __str__(self) -> str:
        return f"PlateIdentity({self.state} {self.number})".strip()

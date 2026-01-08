from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


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

    def __str__(self):
        return f"{self.name} - {self.organization}"

    @property
    def is_parser_running(self) -> bool:
        """Оптимистичная индикация состояния для админки.

        Celery воркер не всегда предоставляет result backend, поэтому ориентируемся на:
          - parser_status == running
          - heartbeat не старше 2 минут (если heartbeat есть)
        """
        if self.parser_status != self.PARSER_STATUS_RUNNING:
            return False
        if not self.parser_heartbeat_at:
            return True
        return self.parser_heartbeat_at >= (timezone.now() - timezone.timedelta(minutes=2))


class Alarm(models.Model):
    monitor_id = models.IntegerField()
    monitor_name = models.CharField(max_length=255)
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

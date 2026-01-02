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
    subscribed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Subscriber {self.chat_id} - {self.username or 'Unknown'}"


class TelegramSubscriberMonitorSubscription(models.Model):
    """Связь many-to-many между TelegramSubscriber и Monitor."""

    subscriber = models.ForeignKey(TelegramSubscriber, on_delete=models.CASCADE)
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE)
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
        verbose_name = "Подписка на отчёты"
        verbose_name_plural = "Подписки на отчёты"

    def __str__(self):
        return f"ReportSubscription(subscriber={self.subscriber_id}, freq={self.frequency})"

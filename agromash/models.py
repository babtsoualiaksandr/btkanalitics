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


class TelegramSubscriber(models.Model):
    chat_id = models.BigIntegerField(unique=True)
    subscribed_monitors = models.JSONField(default=list)
    username = models.CharField(max_length=255, blank=True, null=True)
    subscribed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Subscriber {self.chat_id} - {self.username or 'Unknown'}"


class Monitor(models.Model):
    monitor_id = models.CharField(max_length=255, unique=True)
    monitor_name = models.CharField(max_length=255)
    topic = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.monitor_name} (ID: {self.monitor_id})"

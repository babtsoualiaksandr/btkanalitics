from django.db import models
from django.contrib.auth.models import User


class AccountVideoAnalytics(models.Model):
    name = models.CharField(max_length=255, help_text="Account name")
    password = models.CharField(max_length=255, help_text="Account password")
    contract = models.CharField(max_length=255, help_text="Contract number")
    organization = models.CharField(max_length=255, help_text="Organization name")
    access_token = models.TextField(null=True, blank=True)
    refresh_token = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} - {self.organization}"


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


from django.contrib import admin
from django.core.management import call_command
from .models import  AccountVideoAnalytics, Alarm


def parse_event_action(modeladmin, request, queryset):
    for account in queryset:
        call_command('parse_event', account.name, account.password)

parse_event_action.short_description = "Run parse_event for selected accounts"

@admin.register(AccountVideoAnalytics)
class AccountVideoAnalyticsAdmin(admin.ModelAdmin):
    list_display = ('name', 'password','organization', 'contract')
    search_fields = ('name', 'password', 'organization', 'contract')
    actions = [parse_event_action]


@admin.register(Alarm)
class AlarmAdmin(admin.ModelAdmin):
    list_display = ('alarm_id', 'topic', 'monitor_name', 'start_time', 'end_time')
    search_fields = ('alarm_id', 'topic', 'monitor_name')
    readonly_fields = ('data',)


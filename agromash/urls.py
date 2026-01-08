from django.urls import path
from . import views
from . import views_tg

urlpatterns = [
    path('start-parsing/', views.start_parsing, name='start_parsing'),
    path('snapshot/<str:alarm_id>/', views.serve_snapshot, name='serve_snapshot'),
    path('system-status/', views.system_status, name='system_status'),

    # Telegram Mini App
    path('tg/', views_tg.tg_app, name='tg_app'),
    path('tg/api/subscriptions/', views_tg.tg_api_subscriptions, name='tg_api_subscriptions'),
    path('tg/api/subscriptions/<int:subscription_id>/update/', views_tg.tg_api_update_subscription, name='tg_api_update_subscription'),
    path('tg/api/subscriptions/<int:subscription_id>/send-now/', views_tg.tg_api_send_now, name='tg_api_send_now'),
    path('tg/api/subscriptions/<int:subscription_id>/send-range/', views_tg.tg_api_send_range, name='tg_api_send_range'),

    # Alarm notifications (allowed monitors list + enable/disable)
    path('tg/api/alarm-monitors/', views_tg.tg_api_alarm_monitors, name='tg_api_alarm_monitors'),
    path('tg/api/alarm-monitors/<int:monitor_pk>/set-enabled/', views_tg.tg_api_alarm_monitor_set_enabled, name='tg_api_alarm_monitor_set_enabled'),
]

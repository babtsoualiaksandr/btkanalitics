from django.urls import path
from . import views
from . import views_tg
from . import views_events
from . import views_fuel_report

urlpatterns = [
    path('snapshot/<str:alarm_id>/', views.serve_snapshot, name='serve_snapshot'),
    path('system-status/', views.system_status, name='system_status'),

    # Отчёты о заправках (не-admin страница для операторов)
    path('fuel-reports/', views_fuel_report.fuel_report_list, name='fuel_report_list'),
    path('fuel-reports/upload/', views_fuel_report.fuel_report_upload, name='fuel_report_upload'),
    path('fuel-reports/<int:report_id>/analyze/', views_fuel_report.fuel_report_analyze, name='fuel_report_analyze'),
    path('fuel-reports/<int:report_id>/export/', views_fuel_report.fuel_report_export, name='fuel_report_export'),
    path('fuel-reports/<int:report_id>/download/', views_fuel_report.fuel_report_download, name='fuel_report_download'),
    path('fuel-reports/<int:report_id>/operations/', views_fuel_report.fuel_report_operations, name='fuel_report_operations'),
    path('fuel-reports/<int:report_id>/send/', views_fuel_report.fuel_report_send, name='fuel_report_send'),

    # Events dashboard (non-admin)
    path('events/', views_events.events_list, name='events_list'),
    path('events/table-body/', views_events.events_table_body, name='events_table_body'),
    path('events/export.xlsx', views_events.events_export_xlsx, name='events_export_xlsx'),
    path('events/alarm/<int:alarm_pk>/export.xlsx', views_events.event_export_xlsx, name='event_export_xlsx'),
    path('events/alarm/<int:alarm_pk>/export.pdf', views_events.event_export_pdf, name='event_export_pdf'),
    path('events/alarm/<int:alarm_pk>/case/', views_events.alarm_case_modal, name='alarm_case_modal'),
    path('events/docs/<int:doc_pk>/file/', views_events.alarm_document_file, name='alarm_document_file'),
    path('events/docs/<int:doc_pk>/delete/', views_events.alarm_document_delete, name='alarm_document_delete'),

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

from django.urls import path
from . import views

urlpatterns = [
    path('start-parsing/', views.start_parsing, name='start_parsing'),
    path('snapshot/<str:alarm_id>/', views.serve_snapshot, name='serve_snapshot'),
    path('system-status/', views.system_status, name='system_status'),
]

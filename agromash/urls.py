from django.urls import path
from . import views

urlpatterns = [
    path('start-parsing/', views.start_parsing, name='start_parsing'),
]
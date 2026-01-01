from django.shortcuts import render, redirect
from django.http import HttpResponse, Http404
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
import subprocess
import os
from .models import Alarm

from .va_api_client import VAApiClient

def start_parsing(request):
    if request.method == 'POST':
        pass
        return HttpResponse('Parsing started')
    return render(request, 'agromash/start_parsing.html')


@csrf_exempt
def serve_snapshot(request, alarm_id):
    """
    View для отображения изображения с использованием Bearer токена
    """
    try:
        alarm = Alarm.objects.get(alarm_id=alarm_id)
    except Alarm.DoesNotExist:
        raise Http404("Alarm not found")
    
    if not alarm.original_quality_snapshot or not alarm.account or not alarm.account.access_token:
        raise Http404("No snapshot available")
    
    try:
        client = VAApiClient(account_id=alarm.account_id, base_url=settings.BASE_URL)
        resp = client.request('GET', alarm.original_quality_snapshot, stream=True)

        if resp.status_code != 200:
            resp.close()
            raise Http404("Failed to fetch image")

        content_type = resp.headers.get('content-type', 'image/jpeg')
        content = resp.content
        resp.close()
        return HttpResponse(content, content_type=content_type)
    except Exception as e:
        raise Http404(f"Error fetching image: {str(e)}")

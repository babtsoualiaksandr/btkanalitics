from django.shortcuts import render, redirect
from django.http import HttpResponse, Http404
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
import requests
import subprocess
import os
from .models import Alarm

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
    
    # Формируем URL для изображения
    image_url = f"{settings.BASE_URL}{alarm.original_quality_snapshot}"
    
    try:
        # Делаем запрос к изображению с Bearer токеном
        headers = {
            'Authorization': f'Bearer {alarm.account.access_token}'
        }
        response = requests.get(image_url, headers=headers, stream=True)
        
        if response.status_code == 200:
            # Определяем content-type
            content_type = response.headers.get('content-type', 'image/jpeg')
            
            return HttpResponse(
                response.content,
                content_type=content_type
            )
        else:
            raise Http404("Failed to fetch image")
            
    except Exception as e:
        raise Http404(f"Error fetching image: {str(e)}")

from django.shortcuts import render, redirect
from django.http import HttpResponse
from .models import ParsingTask
import subprocess
import os

def start_parsing(request):
    if request.method == 'POST':
        url = request.POST.get('url')
        selector = request.POST.get('selector')
        if url and selector:
            task, created = ParsingTask.objects.get_or_create(
                url=url,
                window_selector=selector,
                defaults={'is_active': True}
            )
            if not created:
                task.is_active = True
                task.save()
            # Start the management command in background
            # Note: In production, use proper task queue like Celery
            subprocess.Popen(['python', 'manage.py', 'parse_page'], cwd=os.path.dirname(__file__).replace('agromash', ''))
            return HttpResponse('Parsing started')
    return render(request, 'start_parsing.html')

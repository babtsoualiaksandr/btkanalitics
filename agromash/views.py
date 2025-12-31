from django.shortcuts import render, redirect
from django.http import HttpResponse
import subprocess
import os

def start_parsing(request):
    if request.method == 'POST':
        pass
        return HttpResponse('Parsing started')
    return render(request, 'agromash/start_parsing.html')

"""Django admin для agromash, разбит на модули по смысловым областям.

Раньше это был один файл agromash/admin.py на 1546 строк. Django admin
autodiscovery ищет модуль/пакет `admin` внутри каждого приложения — эта
структура (пакет с __init__.py, импортирующим субмодули) работает точно
так же прозрачно, как раньше работал один файл.
"""

from django.contrib import admin

# -----------------
# Django admin: название во вкладке браузера / заголовки
# -----------------
# В стандартных шаблонах Django admin текст во вкладке формируется на основе
# `admin.site.site_title`.
admin.site.site_header = "BTK Analitics"
admin.site.site_title = "BTK Analitics Admin"
admin.site.index_title = "Управление"

# Импорт субмодулей регистрирует все ModelAdmin через @admin.register(...)
# (сам факт импорта — единственное, что здесь нужно).
from . import users  # noqa: E402,F401
from . import accounts  # noqa: E402,F401
from . import alarms  # noqa: E402,F401
from . import telegram  # noqa: E402,F401
from . import fuel_report  # noqa: E402,F401

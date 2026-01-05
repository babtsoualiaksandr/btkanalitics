# btkanalitics

## Зависимости

Проект использует [`decouple.config()`](btkanalitics/settings.py:15) для чтения переменных окружения.

Если при запуске/тестах видите ошибку:

```
ModuleNotFoundError: No module named 'decouple'
```

установите пакет:

```bash
pip install python-decouple
```

## Email отчёты (SMTP)

Для отправки email-отчётов используется стандартный Django email backend (SMTP).
Настройки читаются из окружения в [`btkanalitics/settings.py`](btkanalitics/settings.py:1).

Минимальный набор переменных для Gmail (STARTTLS):

```bash
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=your@gmail.com
EMAIL_HOST_PASSWORD='xxxx xxxx xxxx xxxx'  # Gmail App Password
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL='BTK Reports <your@gmail.com>'
```

## Импорт пооперационных XLSX отчётов

Импорт выполняется через Django admin в модели [`FuelReport`](agromash/models.py:288) (кнопка «Импорт XLSX»).

Для импорта требуется библиотека `openpyxl`:

```bash
pip install openpyxl
```

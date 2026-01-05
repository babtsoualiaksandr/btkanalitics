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

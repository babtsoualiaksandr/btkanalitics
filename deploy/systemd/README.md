# systemd: единый старт/стоп для Django + Celery worker + Celery beat

Файлы unit'ов лежат в каталоге [`deploy/systemd/`](deploy/systemd/README.md:1).

## Установка

1) Скопируйте unit-файлы в `/etc/systemd/system/`:

```bash
sudo cp deploy/systemd/btkanalitics-*.service /etc/systemd/system/
sudo cp deploy/systemd/btkanalitics.target /etc/systemd/system/
```

2) Перечитайте конфиги systemd:

```bash
sudo systemctl daemon-reload
```

3) Включите автозапуск и запустите всё одной командой:

```bash
sudo systemctl enable --now btkanalitics.target
```

## Остановка/перезапуск

```bash
sudo systemctl stop btkanalitics.target
sudo systemctl restart btkanalitics.target
```

## Логи

```bash
journalctl -u btkanalitics-web.service -f
journalctl -u btkanalitics-celery-worker.service -f
journalctl -u btkanalitics-celery-beat.service -f
```

## Важно про virtualenv

В unit'ах сейчас `ExecStart` запускается через `bash -lc`.
Если у вас virtualenv, самый надёжный вариант — указать полный путь к бинарникам venv:

- `python`: `/home/sshbeltelecom/va/venv/bin/python`
- `celery`: `/home/sshbeltelecom/va/venv/bin/celery`

См. [`btkanalitics-web.service`](deploy/systemd/btkanalitics-web.service:1) и остальные unit-файлы.

## Примечание

`manage.py runserver` — это dev-сервер. Для продакшена лучше заменить web unit на gunicorn/uvicorn.

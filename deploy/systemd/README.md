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

### Важно про stop у target

Чтобы `systemctl stop btkanalitics.target` останавливал и сервисы, сервисные unit'ы должны иметь `PartOf=btkanalitics.target` в секции `[Unit]`.
Это настроено в:

- [`btkanalitics-web.service`](deploy/systemd/btkanalitics-web.service:1)
- [`btkanalitics-celery-worker.service`](deploy/systemd/btkanalitics-celery-worker.service:1)
- [`btkanalitics-celery-beat.service`](deploy/systemd/btkanalitics-celery-beat.service:1)

После правок не забудьте:

```bash
sudo cp deploy/systemd/btkanalitics-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart btkanalitics.target
```

Если ранее сервисы включались отдельно (например, `enable btkanalitics-web.service`), то они могут продолжать жить независимо от target.
В таком случае отключите их автозапуск и используйте только target:

```bash
sudo systemctl disable btkanalitics-web.service btkanalitics-celery-worker.service btkanalitics-celery-beat.service
sudo systemctl enable --now btkanalitics.target
```

## Логи

```bash
journalctl -u btkanalitics-web.service -f
journalctl -u btkanalitics-celery-worker.service -f
journalctl -u btkanalitics-celery-beat.service -f
```

Все сервисы (Django/gunicorn, Celery worker/beat) пишут только в stdout/stderr —
своих файлов логов нет, отдельный logrotate.d им не нужен. Единственное место,
где логи реально копятся — сам journald (по умолчанию без лимита размера, у нас
на 2026-08-28 разросся до 4+ ГБ). Лимит задан в
[`journald-btkanalitics.conf`](deploy/systemd/journald-btkanalitics.conf:1)
(1 ГБ / 90 дней), установка:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/systemd/journald-btkanalitics.conf /etc/systemd/journald.conf.d/
sudo systemctl restart systemd-journald
sudo journalctl --vacuum-size=1G
```

nginx и postgresql пишут в файлы и уже покрыты штатными `/etc/logrotate.d/nginx`
и `/etc/logrotate.d/postgresql-common` (ставятся вместе с пакетами) — трогать
не нужно.

## Важно про virtualenv

В unit'ах сейчас `ExecStart` запускается через `bash -lc`.
Если у вас virtualenv, самый надёжный вариант — указать полный путь к бинарникам venv:

- `python`: `/home/sshbeltelecom/va/venv/bin/python`
- `celery`: `/home/sshbeltelecom/va/venv/bin/celery`

См. [`btkanalitics-web.service`](deploy/systemd/btkanalitics-web.service:1) и остальные unit-файлы.

## Примечание

`manage.py runserver` — это dev-сервер. Для продакшена лучше заменить web unit на gunicorn/uvicorn.

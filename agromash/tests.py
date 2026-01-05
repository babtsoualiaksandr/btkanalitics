import datetime

from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from django.test import override_settings
from django.core import mail
from unittest.mock import Mock, patch
from django.contrib.auth.models import User

from agromash.models import AccountVideoAnalytics, Alarm, Monitor, TelegramReportSubscription, TelegramSubscriber
from agromash.services.alarm_data_parser import format_alarm_caption, parse_alarm_data
from agromash.services.report_scheduler import compute_next_run_at
from agromash.services.reporting import get_alarms_for_subscription
from agromash.va_api_client import VAApiClient
from agromash.tasks import send_email_report_now
from agromash.services.fuel_report_importer import import_fuel_report_from_xlsx
from agromash.models import FuelReport, FuelOperation


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_data=None, content: bytes = b"", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def close(self):
        return None


class TelegramSubscriberMonitorM2MTest(TestCase):
    def test_subscribe_unsubscribe_monitor(self):
        monitor = Monitor.objects.create(monitor_id='123', monitor_name='Test Monitor', topic='')
        sub = TelegramSubscriber.objects.create(chat_id=1, username='u')

        sub.subscribed_monitors.add(monitor)
        self.assertEqual(sub.subscribed_monitors.count(), 1)
        self.assertEqual(monitor.subscribers.count(), 1)

        sub.subscribed_monitors.remove(monitor)
        self.assertEqual(sub.subscribed_monitors.count(), 0)
        self.assertEqual(monitor.subscribers.count(), 0)


class AlarmDataParserTest(TestCase):
    def test_parse_line_crossed(self):
        payload = {
            "id": "1287:1767348071339:22653779894376786",
            "topic": "LineCrossed",
            "level": 3,
            "monitor_id": 258,
            "monitor_name": "Бобруйск, Пришли купить в Shop",
            "channel_id": 1287,
            "channel_name": "РУП Белтелеком ...",
            "start_time": 1767348071339,
            "end_time": 1767348071339,
            "event_id": 22653779894376786,
            "tags": [{"id": 53, "name": "технологический"}],
            "params": {
                "object": {
                    "id": 3986,
                    "classes": [
                        {"class": "human", "similarity": 0.9},
                        {"class": "vehicle", "similarity": 0.1},
                    ],
                    "bounding_box": {"top": 1, "left": 2, "right": 3, "bottom": 4},
                },
                "snapshots": [
                    {"metadata": {"tripwire": [{"x": 0.1, "y": 0.2}]}}
                ],
            },
            "snapshots": [
                {
                    "tag": "initial",
                    "path": "/api/v2/...",
                    "type": "FULLSCREEN",
                    "original_quality_snapshot": "/api/v2/.../original",
                }
            ],
        }
        parsed = parse_alarm_data(payload)
        self.assertEqual(parsed.topic, "LineCrossed")
        self.assertEqual(parsed.monitor_id, 258)
        self.assertEqual(parsed.details.get("best_class"), "human")
        self.assertTrue(parsed.original_quality_snapshot)

    def test_parse_plate_not_matched(self):
        payload = {
            "id": "1857:1767348088024:32681325944115337",
            "topic": "PlateNotMatched",
            "level": 1,
            "monitor_id": 340,
            "params": {
                "plate": {"state": "BY", "valid": True, "number": "1399ip6", "recognition_time": 1767348088185},
                "object": {"id": 714, "object_type": {"value": "vehicle"}, "bounding_box": {"top": 0}},
                "identities": [],
            },
        }
        parsed = parse_alarm_data(payload)
        self.assertEqual(parsed.topic, "PlateNotMatched")
        self.assertEqual(parsed.details.get("plate_number"), "1399ip6")
        self.assertEqual(parsed.details.get("object_type"), "vehicle")

    def test_parse_face_not_matched(self):
        payload = {
            "id": "741:1767339105702:13048444018989602",
            "topic": "FaceNotMatched",
            "level": 1,
            "monitor_id": 149,
            "params": {
                "object": {"id": 8994, "bounding_box": {"top": 0.1}},
                "rotation": {"yaw": -2.0, "roll": 1.6, "pitch": -5.4},
                "attributes": {"age": 43, "gender": "female"},
                "scrfd_enabled": True,
                "face_attribute_enabled": True,
            },
        }
        parsed = parse_alarm_data(payload)
        self.assertEqual(parsed.topic, "FaceNotMatched")
        self.assertEqual(parsed.details.get("attributes", {}).get("age"), 43)

    def test_parse_plate_matched_includes_identities_and_owner(self):
        payload = {
            "id": "1857:1767416611422:32681343486071549",
            "topic": "PlateMatched",
            "level": 1,
            "monitor_id": 332,
            "params": {
                "plate": {"state": "BY", "valid": True, "number": "am18686", "recognition_time": 1767416553344},
                "identities": [
                    {
                        "list": {"id": 104, "name": "БУЭС", "level": 1},
                        "plates": [
                            {
                                "id": 1025,
                                "state": "BY",
                                "number": "AM18686",
                                "owner_last_name": "Гусейнов",
                                "owner_first_name": "Н.",
                                "owner_middle_name": "802003121",
                            }
                        ],
                    }
                ],
            },
        }

        parsed = parse_alarm_data(payload)
        self.assertEqual(parsed.topic, "PlateMatched")
        self.assertEqual(parsed.details.get("matched_list_name"), "БУЭС")
        self.assertEqual(parsed.details.get("matched_plate", {}).get("state"), "BY")
        self.assertEqual(parsed.details.get("matched_plate", {}).get("number"), "AM18686")
        self.assertEqual(parsed.details.get("matched_plate", {}).get("owner_last_name"), "Гусейнов")
        self.assertEqual(parsed.details.get("matched_plate", {}).get("owner_first_name"), "Н.")
        self.assertEqual(parsed.details.get("matched_plate", {}).get("owner_middle_name"), "802003121")

        caption = format_alarm_caption(parsed)
        self.assertIn("PlateMatched", caption)
        self.assertIn("list=БУЭС", caption)
        self.assertIn("plate=BY AM18686", caption)
        self.assertIn("owner=Гусейнов Н. 802003121", caption)


class ReportSubscriptionTest(TestCase):
    def test_compute_next_run_at_hourly(self):
        now = timezone.now()
        nxt = compute_next_run_at(now=now, frequency="hourly")
        self.assertTrue(nxt > now)

    def test_get_alarms_for_subscription_filters_by_time_and_monitor(self):
        now = timezone.now()
        now_ms = int(now.timestamp() * 1000)

        acc = AccountVideoAnalytics.objects.create(
            name="n",
            password="p",
            contract="c",
            organization="o",
        )
        sub = TelegramSubscriber.objects.create(chat_id=123, username="u")
        mon = Monitor.objects.create(monitor_id="258", monitor_name="M", topic="")

        report = TelegramReportSubscription.objects.create(
            subscriber=sub,
            period_from_minutes=60,
            period_to_minutes=0,
            frequency=TelegramReportSubscription.FREQ_HOURLY,
        )
        report.monitors.add(mon)

        Alarm.objects.create(
            monitor_id=258,
            monitor_name="M",
            alarm_id="a1",
            topic="LineCrossed",
            start_time=now_ms - 30 * 60_000,
            end_time=now_ms - 30 * 60_000,
            event_id=1,
            data={"topic": "LineCrossed"},
            account=acc,
        )
        # другой монитор — не должен попасть
        Alarm.objects.create(
            monitor_id=999,
            monitor_name="X",
            alarm_id="a2",
            topic="LineCrossed",
            start_time=now_ms - 30 * 60_000,
            end_time=now_ms - 30 * 60_000,
            event_id=2,
            data={"topic": "LineCrossed"},
            account=acc,
        )
        # слишком старый — не должен попасть
        Alarm.objects.create(
            monitor_id=258,
            monitor_name="M",
            alarm_id="a3",
            topic="LineCrossed",
            start_time=now_ms - 5 * 60 * 60_000,
            end_time=now_ms - 5 * 60 * 60_000,
            event_id=3,
            data={"topic": "LineCrossed"},
            account=acc,
        )

        qs = get_alarms_for_subscription(sub=report, now=now)
        self.assertEqual(qs.count(), 1)


class VAApiClientBootstrapAuthTest(TestCase):
    def test_request_when_tokens_missing_logs_in_and_persists_tokens(self):
        acc = AccountVideoAnalytics.objects.create(
            name="login",
            password="pass",
            contract="c",
            organization="o",
            access_token=None,
            refresh_token=None,
        )

        session = Mock()
        session.post.return_value = _FakeResponse(
            status_code=200,
            json_data={"access_token": "A", "refresh_token": "R"},
        )
        session.request.return_value = _FakeResponse(status_code=200, json_data={"ok": True})

        client = VAApiClient(account_id=acc.id, base_url="https://example.test", session=session)
        resp = client.request("GET", "/api/v1/ping")
        self.assertEqual(resp.status_code, 200)

        acc.refresh_from_db()
        self.assertEqual(acc.access_token, "A")
        self.assertEqual(acc.refresh_token, "R")

        # Первый запрос должен уйти уже с Bearer-токеном (без лишнего 401 на старте).
        _, kwargs = session.request.call_args
        self.assertIn("headers", kwargs)
        self.assertEqual(kwargs["headers"].get("Authorization"), "Bearer A")

        # login должен быть выполнен ровно один раз
        self.assertEqual(session.post.call_count, 1)


class ServeSnapshotBootstrapAuthTest(TestCase):
    def test_serve_snapshot_does_not_require_access_token_in_db(self):
        acc = AccountVideoAnalytics.objects.create(
            name="login",
            password="pass",
            contract="c",
            organization="o",
            access_token=None,
            refresh_token=None,
        )
        alarm = Alarm.objects.create(
            monitor_id=1,
            monitor_name="m",
            alarm_id="a1",
            topic="t",
            start_time=1,
            end_time=1,
            event_id=1,
            original_quality_snapshot="/api/v2/snap/original",
            data={"topic": "t"},
            account=acc,
        )

        with patch("agromash.views.VAApiClient.request") as req_mock:
            req_mock.return_value = _FakeResponse(
                status_code=200,
                content=b"img",
                headers={"content-type": "image/jpeg"},
            )
            url = reverse("serve_snapshot", args=[alarm.alarm_id])
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.content, b"img")


class EmailReportTest(TestCase):
    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_send_email_report_now_sends_email_with_attachments(self):
        sub_user = TelegramSubscriber.objects.create(chat_id=123, username="u")
        sub = TelegramReportSubscription.objects.create(
            subscriber=sub_user,
            email="to@example.test",
            period_from_minutes=60,
            period_to_minutes=0,
            frequency=TelegramReportSubscription.FREQ_HOURLY,
            enabled=True,
        )

        with patch('agromash.tasks.generate_report_attachments') as gen:
            gen.return_value = (
                "caption",
                [("r.txt", b"hi", "text/plain")],
                1,
            )
            send_email_report_now.run(sub.id, source="test")

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["to@example.test"])
        self.assertIn("caption", msg.body)
        self.assertEqual(len(msg.attachments), 1)


class FuelReportImportTest(TestCase):
    def test_import_fuel_report_from_xlsx_skips_totals_and_propagates_card(self):
        try:
            import openpyxl
        except Exception:
            self.skipTest("openpyxl is not installed")

        from io import BytesIO

        wb = openpyxl.Workbook()
        ws = wb.active

        # meta header
        ws.append(["Пооперационный_отчёт_01.12.2025_31.12.2025"])
        ws.append(["По договору № 4117"])
        ws.append(["Наименование организации: Test Org"])
        ws.append([])

        # real header row
        header = [
            "Номер топливной карты",
            "Номер подразделения",
            "Дата и время отпуска",
            "Наименование товара/услуги",
            "Код товара/услуги",
            "Количество",
            "Единица измерения",
            "Цена единицы с НДС со скидкой, руб. коп.",
            "Стоимость с НДС со скидкой, руб. коп.",
            "НДС, руб. коп.",
            "Скидка с НДС, руб. коп.",
            "% за услуги",
            "Стоимость услуг с НДС, руб. коп.",
            "Стоимость всего с НДС, руб. коп.",
            "Сумма НДС всего, руб. коп.",
            "Владелец точки обслуживания",
            "Номер точки обслуживания",
            "Номер ТРК/номер секции",
            "Фио водителя",
            "Номер транспортного средства",
        ]
        ws.append(header)

        # row with card
        ws.append([
            "802003108",
            "3",
            "2025-12-02 11:58:08",
            "Газ",
            "16",
            31.33,
            "л.",
            1.32,
            41.36,
            6.89,
            0,
            0,
            0,
            41.36,
            6.89,
            "МогилевОНП",
            "64",
            "5",
            "",
            "",
        ])
        # row without card => should propagate 802003108
        ws.append([
            "",
            "3",
            "2025-12-08 07:55:54",
            "АИ-95-Евро",
            "3",
            20,
            "л.",
            2.6,
            52,
            8.67,
            0,
            0,
            0,
            52,
            8.67,
            "МогилевОНП",
            "22",
            "4",
            "",
            "",
        ])
        # totals row => should be skipped
        ws.append(["Итого по 802003108"])

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)

        u = User.objects.create_user(username="u")
        res = import_fuel_report_from_xlsx(
            file_obj=bio,
            filename="test.xlsx",
            imported_by=u,
            period_start=datetime.date(2025, 12, 1),
            period_end=datetime.date(2025, 12, 31),
        )
        self.assertEqual(res.created_rows, 2)
        self.assertEqual(FuelReport.objects.count(), 1)
        self.assertEqual(FuelOperation.objects.count(), 2)

        op2 = FuelOperation.objects.order_by('operation_at').last()
        self.assertEqual(op2.card_number, "802003108")

import os
from django.core.management.base import BaseCommand
from django.conf import settings
from agromash.models import AccountVideoAnalytics, Alarm
import requests
import json
import time


class Command(BaseCommand):
    help = 'Parse events for a specific account'

    def add_arguments(self, parser):
        parser.add_argument('name', type=str, help='Account name')
        parser.add_argument('password', type=str, help='Account password')

    def handle(self, *args, **options):
        name = options['name']
        password = options['password']
        base_url = settings.BASE_URL
        self.stdout.write(f'Using BASE_URL: {base_url}')
        try:
            account = AccountVideoAnalytics.objects.get(name=name, password=password)
            self.stdout.write(f'Processing account: {account.name} for {account.organization}')
            self.run_parsing(account, base_url)
        except AccountVideoAnalytics.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Account with name {name} and password {password} not found'))

    def run_parsing(self, account, base_url):
        while True:
            try:
                # Authenticate
                self.stdout.write('Authenticating...')
                auth_url = f"{base_url}/oauth2/v1/auth/authenticate"
                payload = {
                    "name": account.name,
                    "password": account.password,
                    "rememberme": True
                }
                response = requests.post(auth_url, json=payload)
                if response.status_code != 200:
                    self.stdout.write(self.style.ERROR(f'Authentication failed: {response.status_code} {response.text}'))
                    continue
                data = response.json()
                access_token = data.get('access_token')
                refresh_token = data.get('refresh_token')
                if not access_token:
                    self.stdout.write(self.style.ERROR('No access_token in response'))
                    continue
                account.access_token = access_token
                account.refresh_token = refresh_token
                account.save()
                self.stdout.write('Authentication successful, got access_token')

                # Listen to SSE with reconnects
                while True:
                    self.listen_sse(base_url, access_token, account)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error in run_parsing: {e}'))

    def listen_sse(self, base_url, access_token, account):
        sse_url = f"{base_url}/sse-holder/api/v1/sse?platform=WEB&ngsw-bypass"
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'text/event-stream',
            'Cache-Control': 'no-cache',
        }
        try:
            response = requests.get(sse_url, headers=headers, stream=True)
            response.raise_for_status()
            event_type = None
            for line in response.iter_lines(decode_unicode=True):
                self.stdout.write(f'line {line} !')
                part_line = line[0:6]
                self.stdout.write(f'line @{part_line}@')
                if line == '':
                    # Process the event
                    if event_type and data:
                        #data = '\n'.join(data_lines)
                        if event_type == "KEEP_ALIVE":
                            try:
                                parsed_data = json.loads(data)
                                ttl = parsed_data.get('ttl_seconds', 0)
                                self.stdout.write(f'TTL == {ttl} ')
                                if ttl < 30:
                                    self.stdout.write(f'TTL {ttl} < 30, restarting stream')
                                    return
                                else:
                                    self.stdout.write(f'KEEP_ALIVE: TTL {ttl}')
                            except json.JSONDecodeError:
                                self.stdout.write(f'Invalid JSON in KEEP_ALIVE: {data}')
                        elif event_type == "ALARM_MONITOR":
                            try:
                                parsed_data = json.loads(data)
                                # parse further
                                monitor = parsed_data.get('monitor', {})
                                monitor_id = monitor.get('id')
                                monitor_name = monitor.get('name')
                                self.stdout.write(f'ALARM_MONITOR: ID {monitor_id}, Name {monitor_name}')
                                if monitor_id:
                                    try:
                                        alarm_url = f"{base_url}/api/v2/alarm-monitors/{monitor_id}/alarms/search"
                                        headers = {
                                            'Authorization': f'Bearer {access_token}',
                                            'Content-Type': 'application/json'
                                        }
                                        payload = {"size": 2}
                                        response = requests.post(alarm_url, headers=headers, json=payload)
                                        if response.status_code == 200:
                                            alarms = response.json()
                                            saved_count = 0
                                            for alarm in alarms:
                                                _, created = Alarm.objects.get_or_create(
                                                    alarm_id=alarm['id'],
                                                    account=account,
                                                    defaults={
                                                        'monitor_id': alarm['monitor_id'],
                                                        'monitor_name': alarm['monitor_name'],
                                                        'topic': alarm['topic'],
                                                        'start_time': alarm['start_time'],
                                                        'end_time': alarm['end_time'],
                                                        'event_id': alarm['event_id'],
                                                        'original_quality_snapshot': alarm.get('original_quality_snapshot'),
                                                        'plate_identities': alarm.get('plate_identities'),
                                                        'face_identities': alarm.get('face_identities'),
                                                        'snapshots': alarm.get('snapshots'),
                                                        'data': alarm
                                                    }
                                                )
                                                if created:
                                                    saved_count += 1
                                            self.stdout.write(f'Saved {saved_count} new alarms for monitor {monitor_id}')
                                        else:
                                            self.stdout.write(f'Failed to get alarms for {monitor_id}: {response.status_code} {response.text}')
                                    except Exception as e:
                                        self.stdout.write(f'Error getting alarms for {alarm}  {monitor_id}: {e}')
                            except json.JSONDecodeError:
                                self.stdout.write(f'Invalid JSON in ALARM_MONITOR: {data}')
                        else:
                            self.stdout.write(f'Unknown event type: {event_type}, data: {data}')
                    event_type = None
                    data_lines = []
                if line.startswith('event:'):
                    event_type = line[6:]
                    self.stdout.write(f'event_type: {event_type}')
                if line.startswith('data:'):
                    data = line[5:]
                    self.stdout.write(f' data: {data}')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error in SSE: {e}'))
            return False
        return False
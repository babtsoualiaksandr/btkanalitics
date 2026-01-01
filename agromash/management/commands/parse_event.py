from django.core.management.base import BaseCommand
from django.conf import settings

from agromash.models import AccountVideoAnalytics
from agromash.services.parse_event_runner import ParserRunContext, run_parse_event


class Command(BaseCommand):
    help = 'Parse events for a specific account'

    def add_arguments(self, parser):
        parser.add_argument('name', type=str, help='Account name')
        parser.add_argument('password', type=str, help='Account password')

    def handle(self, *args, **options):
        name = options['name']
        password = options['password']
        try:
            account = AccountVideoAnalytics.objects.get(name=name, password=password)
            self.stdout.write(f'Processing account: {account.name} for {account.organization}')

            run_parse_event(
                account_id=account.id,
                task_id=None,
                ctx=ParserRunContext(account_id=account.id, base_url=settings.BASE_URL),
                stdout_write=self.stdout.write,
            )
        except AccountVideoAnalytics.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Account with name {name} and password {password} not found'))

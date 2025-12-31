from django.core.management.base import BaseCommand
from agromash.models import ParsingTask, Event
import time
import json
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

class Command(BaseCommand):
    help = 'Run continuous page parsing for active tasks'

    def handle(self, *args, **options):
        tasks = ParsingTask.objects.filter(is_active=True)
        if not tasks:
            self.stdout.write('No active parsing tasks.')
            return

        for task in tasks:
            self.stdout.write(f'Starting parser for {task.monitoring_url}')
            self.run_parser(task)

    def run_parser(self, task):
        options = Options()
        options.add_argument('--headless')  # Run in headless mode
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        driver = webdriver.Chrome(options=options)  # Assuming Chrome is installed

        try:
            # Login and get token if not present
            if not task.token:
                self.login_and_get_token(driver, task)

            # Go to main page to set auth
            driver.get(task.url.split('#')[0])
            # Add token and other localStorage items
            driver.execute_script(f"localStorage.setItem('kx:OAuth2AccessTokenStorageKey', '{task.token}');")
            driver.execute_script(f"localStorage.setItem('kx:OAuth2RefreshTokenStorageKey', '{task.token}');")  # Assuming same
            driver.execute_script("localStorage.setItem('kx:alarm-monitors-dashboard-selected', '149');")
            driver.execute_script("localStorage.setItem('kx:alarm-monitors-hide-read-events', 'true');")
            driver.execute_script("localStorage.setItem('kx:caseId', 'null');")
            driver.execute_script("localStorage.setItem('kx:channels-map-position', '{\"points\":[{\"lat\":53.91121454964488,\"lng\":29.78530883789063},{\"lat\":53.86224615767578,\"lng\":30.77407836914063}]}');")
            driver.execute_script("localStorage.setItem('kx:events-active-tab:223', '\"all\"');")
            driver.execute_script("localStorage.setItem('kx:filters-criteria:223', '{\"all\":[\"eventFiltersLocationGroup\"],\"other\":null,\"persons\":null,\"vehicle\":null}');")
            driver.execute_script("localStorage.setItem('kx:locale', '\"ru\"');")
            driver.execute_script("localStorage.setItem('kx:playerType', '\"new\"');")
            driver.execute_script("localStorage.setItem('kx:readEventSearchResultListStorageKey', '[\"656dc712-dcda-4ce4-bf2e-12ff303d5c3d\",\"64cadc07-24f7-450c-b477-5077db02d59d\"]');")
            driver.execute_script("localStorage.setItem('kx:selected-map-layout', '\"CHANNELS\"');")
            driver.execute_script("localStorage.setItem('kx:theme', 'Light');")
            time.sleep(20)

            # Go to alarms page
            alarms_url = f"{task.url.split('#')[0]}#/alarms"
            print(f'Going to URL: {alarms_url}')
            driver.get(alarms_url)
            print(f'Current URL after get: {driver.current_url}')
            print(driver.save_screenshot("out1.png") )
            # Wait for page to load content
            try:
                time.sleep(20)
                print(driver.save_screenshot("out2.png") )
                WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".kp-monitors-dashboard__list"))
                )
                print('Monitors list loaded')
            except TimeoutException:
                print('Monitors list not loaded within 30s')
                driver.save_screenshot('debug_screenshot.png')
                print('Screenshot saved as debug_screenshot.png')
                print('Page source:', driver.page_source[:1000])  # Debug
                return  # Exit if not loaded

            print('Page loaded, selecting monitor')

            # Select alarm monitor
            self.select_alarm_monitor(driver, task)

            # Now listen to SSE for events
            self.listen_to_sse(task, driver)
        except KeyboardInterrupt:
            self.stdout.write('Stopping parser...')
        finally:
            driver.quit()

    def login_and_get_token(self, driver, task):
        # Send POST request to authenticate
        auth_url = f"{task.url.split('#')[0].rstrip('/')}/oauth2/v1/auth/authenticate"
        print(auth_url)
        payload = {
            "name": task.username,
            "password": task.password,
            "rememberme": True
        }
        print(payload)
        import requests
        response = requests.post(auth_url, json=payload)
        if response.status_code == 200:
            data = response.json()
            access_token = data.get('access_token')
            print(access_token)
            if access_token:
                task.token = access_token
                task.save()
                # Set token in localStorage for the page
                driver.get(task.login_url)  # Load page to set localStorage
                driver.execute_script(f"localStorage.setItem('access_token', '{access_token}');")
                driver.execute_script(f"localStorage.setItem('refresh_token', '{data.get('refresh_token', '')}');")
                self.stdout.write('Token retrieved and saved')
            else:
                self.stdout.write('Access token not in response')
        else:
            self.stdout.write(f'Authentication failed: {response.status_code} {response.text}')

    def parse_events(self, driver, task):
        try:
            events_container = driver.find_element(By.CSS_SELECTOR, task.events_container_selector)
            event_items = events_container.find_elements(By.CSS_SELECTOR, 'syn-card-list-item')

            for item in event_items:
                event_data = {
                    'text': item.text,
                    'html': item.get_attribute('outerHTML'),
                    # Add more parsing as needed
                }
                Event.objects.create(parsing_task=task, event_data=event_data)
                self.stdout.write(f'Parsed event: {event_data["text"][:50]}...')

        except NoSuchElementException:
            self.stdout.write('Events container not found')

    def select_alarm_monitor(self, driver, task):
        print(f'Looking for monitors list with selector: {task.monitors_list_selector}')
        # Wait for monitors list
        print(driver.page_source)

        monitors_list = WebDriverWait(driver, 20).until( EC.visibility_of_element_located( (By.CSS_SELECTOR, ".kp-monitors-dashboard__list") ) )
        print('Monitors list found')
        # Find monitor by name
        monitor_items = monitors_list.find_elements(By.CLASS_NAME, 'kp-monitors-dashboard__list-item')
        print(f'Found {len(monitor_items)} monitor items')
        for item in monitor_items:
            name_elements = item.find_elements(By.CLASS_NAME, 'syn-text-ellipsis')
            if name_elements:
                name_element = name_elements[0]  # Assuming first one
                print(f'Checking monitor: {name_element.text}')
                if name_element.text == task.alarm_monitor_name:
                    item.click()
                    self.stdout.write(f'Selected monitor: {task.alarm_monitor_name}')
                    return
        self.stdout.write(f'Monitor {task.alarm_monitor_name} not found')

    def listen_to_sse(self, task, driver):
        import requests
        sse_url = f"{task.url.split('#')[0]}sse-holder/api/v1/sse?platform=WEB&ngsw-bypass"
        headers = {
            'Authorization': f'Bearer {task.token}',
            'Accept': 'text/event-stream',
            'Cache-Control': 'no-cache',
        }
        try:
            with requests.get(sse_url, headers=headers, stream=True) as response:
                if response.status_code == 200:
                    for line in response.iter_lines():
                        if line:
                            line = line.decode('utf-8')
                            if line.startswith('data: '):
                                data = line[6:]
                                try:
                                    event = json.loads(data)
                                    if event.get('type') == 'EVENTS_DETECTED':
                                        self.stdout.write('New events detected via SSE!')
                                        # Parse events using Selenium
                                        self.parse_new_events(task, driver)
                                except json.JSONDecodeError:
                                    pass
                else:
                    self.stdout.write(f'SSE connection failed: {response.status_code}')
        except Exception as e:
            self.stdout.write(f'SSE error: {e}')

    def parse_new_events(self, task, driver):
        # Use the driver to parse events
        self.stdout.write('Parsing new events...')
        try:
            # Click 'New events' button
            new_events_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, task.new_events_button_selector))
            )
            new_events_button.click()
            time.sleep(2)

            # Parse events
            self.parse_events(driver, task)

            # Click 'Read all' button
            try:
                read_all_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, task.read_all_button_selector))
                )
                read_all_button.click()
                self.stdout.write('Clicked Read all')
            except TimeoutException:
                self.stdout.write('Read all button not found')

        except Exception as e:
            self.stdout.write(f'Error parsing events: {e}')
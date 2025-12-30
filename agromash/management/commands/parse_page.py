from django.core.management.base import BaseCommand
from agromash.models import ParsingTask
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

class Command(BaseCommand):
    help = 'Run continuous page parsing for active tasks'

    def handle(self, *args, **options):
        tasks = ParsingTask.objects.filter(is_active=True)
        if not tasks:
            self.stdout.write('No active parsing tasks.')
            return

        # For simplicity, run one task; in real scenario, use threading or multiprocessing
        for task in tasks:
            self.stdout.write(f'Starting parser for {task.url}')
            self.run_parser(task)

    def run_parser(self, task):
        # Placeholder: install selenium and webdriver-manager if needed
        # from webdriver_manager.chrome import ChromeDriverManager
        # driver = webdriver.Chrome(ChromeDriverManager().install())
        driver = webdriver.Chrome()  # Assuming Chrome is installed

        try:
            driver.get(task.url)
            while True:  # Continuous mode
                try:
                    # Wait for the window to appear
                    element = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, task.window_selector))
                    )
                    self.stdout.write(f'Window appeared on {task.url}')
                    # Placeholder: do something, e.g., extract data
                    # For now, just log
                    time.sleep(5)  # Wait a bit before checking again
                except TimeoutException:
                    self.stdout.write('Window not found, waiting...')
                    time.sleep(10)  # Wait before retrying
        except KeyboardInterrupt:
            self.stdout.write('Stopping parser...')
        finally:
            driver.quit()
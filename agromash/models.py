from django.db import models

class ParsingTask(models.Model):
    url = models.URLField(help_text="URL of the page to parse")
    window_selector = models.CharField(max_length=255, help_text="CSS selector for the window to wait for")
    is_active = models.BooleanField(default=False, help_text="Whether the task is currently running")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ParsingTask for {self.url} - {'Active' if self.is_active else 'Inactive'}"

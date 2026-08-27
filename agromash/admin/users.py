from django.contrib import admin
from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm as DjangoUserChangeForm
from django.contrib.auth.models import User

from ..models import Monitor, UserMonitorAccess


# -----------------
# Django admin: удобная установка пароля для Users (Authentication and Authorization)
# -----------------
class UserChangeFormWithPassword(DjangoUserChangeForm):
    """Добавляет поля для установки нового пароля прямо на странице редактирования User."""

    new_password1 = forms.CharField(
        label="Новый пароль",
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text="Если оставить пустым — пароль не изменится.",
    )
    new_password2 = forms.CharField(
        label="Повторите пароль",
        widget=forms.PasswordInput(render_value=False),
        required=False,
    )

    # --- Доступ к мониторам (через UserMonitorAccess) ---
    all_monitors = forms.BooleanField(
        label="Доступ ко всем мониторам",
        required=False,
        help_text="Если включено — пользователю будут назначены все мониторы.",
    )
    monitors = forms.ModelMultipleChoiceField(
        label="Мониторы",
        queryset=Monitor.objects.all().order_by("monitor_id"),
        required=False,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                "class": "agromash-subscribed-monitors",
            }
        ),
        help_text="Выберите один или несколько мониторов для просмотра событий.",
    )

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("new_password1")
        p2 = cleaned.get("new_password2")

        # Оба поля пустые => пароль не меняем.
        if not p1 and not p2:
            return cleaned

        if p1 != p2:
            raise forms.ValidationError("Пароли не совпадают")

        # Учитываем настройки валидаторов паролей Django.
        password_validation.validate_password(p1, self.instance)
        return cleaned

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Инициализация списка мониторов из UserMonitorAccess
        if self.instance and self.instance.pk:
            qs = Monitor.objects.filter(
                user_accesses__user=self.instance,
                user_accesses__enabled=True,
            ).order_by("monitor_id")
            self.fields["monitors"].initial = list(qs)
            # Если назначены все мониторы — выставим флаг (best-effort)
            try:
                self.fields["all_monitors"].initial = (qs.count() == Monitor.objects.count())
            except Exception:
                self.fields["all_monitors"].initial = False

    def save(self, commit=True):
        user = super().save(commit=False)
        p1 = self.cleaned_data.get("new_password1")
        if p1:
            user.set_password(p1)

        if commit:
            user.save()
            self.save_m2m()

            # синхронизируем доступ к мониторам
            if user.pk:
                all_monitors = bool(self.cleaned_data.get("all_monitors"))
                monitors = (
                    list(Monitor.objects.all().order_by("monitor_id"))
                    if all_monitors
                    else list(self.cleaned_data.get("monitors") or [])
                )
                UserMonitorAccess.objects.filter(user=user).delete()
                UserMonitorAccess.objects.bulk_create(
                    [UserMonitorAccess(user=user, monitor=m, enabled=True) for m in monitors]
                )
        return user


class UserAdminWithPassword(DjangoUserAdmin):
    form = UserChangeFormWithPassword

    # Добавляем блок с полями пароля в форму редактирования.
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Смена пароля",
            {
                "fields": (
                    "new_password1",
                    "new_password2",
                )
            },
        ),
        (
            "Доступ к мониторам",
            {
                "fields": (
                    "all_monitors",
                    "monitors",
                )
            },
        ),
    )


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass
admin.site.register(User, UserAdminWithPassword)

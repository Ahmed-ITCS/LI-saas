from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth.models import User
from .models import LinkedInProfile
from .crypto import encrypt, decrypt


class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")


class LoginForm(AuthenticationForm):
    pass


class LinkedInProfileForm(forms.ModelForm):
    """
    We display plaintext fields for email/password and encrypt on save.
    We also display gemini_keys as a plain textarea.
    """
    li_email_plain    = forms.EmailField(label="LinkedIn Email")
    li_password_plain = forms.CharField(label="LinkedIn Password", widget=forms.PasswordInput(render_value=True))

    class Meta:
        model  = LinkedInProfile
        fields = [
            "label",
            "llm_provider",
            "gemini_keys",
            "min_post_age_minutes",
            "max_post_age_minutes",
            "max_comments_per_round",
            "persona_prompt",
            "run_parallel",
        ]
        widgets = {
            "persona_prompt": forms.Textarea(attrs={"rows": 3}),
            "gemini_keys":    forms.Textarea(attrs={"rows": 4, "placeholder": "One key per line"}),
        }
        labels = {
            "gemini_keys": "Gemini API Keys (one per line)",
            "run_parallel": "Run independently (parallel)",
        }

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)
        if instance and instance.pk:
            try:
                self.fields["li_email_plain"].initial    = decrypt(instance.li_email)
                self.fields["li_password_plain"].initial = decrypt(instance.li_password)
            except Exception:
                pass

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.li_email    = encrypt(self.cleaned_data["li_email_plain"])
        obj.li_password = encrypt(self.cleaned_data["li_password_plain"])
        if commit:
            obj.save()
        return obj

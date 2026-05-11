from django.db import migrations, models


def forwards_migrate_mock_to_gemini(apps, schema_editor):
    LinkedInProfile = apps.get_model("accounts", "LinkedInProfile")
    LinkedInProfile.objects.filter(llm_provider="mock").update(llm_provider="gemini")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_make_post_age_optional"),
    ]

    operations = [
        migrations.RunPython(forwards_migrate_mock_to_gemini, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="linkedinprofile",
            name="llm_provider",
            field=models.CharField(
                choices=[("gemini", "Gemini")],
                default="gemini",
                max_length=20,
            ),
        ),
    ]

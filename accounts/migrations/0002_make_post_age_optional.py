from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='linkedinprofile',
            name='min_post_age_minutes',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='linkedinprofile',
            name='max_post_age_minutes',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]

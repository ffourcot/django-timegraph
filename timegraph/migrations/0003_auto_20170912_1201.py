# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('timegraph', '0002_auto_20150622_0644'),
    ]

    operations = [
        migrations.AlterField(
            model_name='metric',
            name='parameter',
            field=models.CharField(max_length=256, verbose_name=b'parameter', db_index=True),
            preserve_default=True,
        ),
    ]

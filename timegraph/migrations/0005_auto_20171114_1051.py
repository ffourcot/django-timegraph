# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-11-14 10:51
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('timegraph', '0004_metric_cache_timeout'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='metric',
            options={'verbose_name': 'metric', 'verbose_name_plural': 'metrics'},
        ),
    ]
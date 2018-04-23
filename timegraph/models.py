# -*- coding: utf-8 -*-
#
# django-timegraph - monitoring graphs for django
# Copyright (c) 2011-2012, Wifirst
# Copyright (c) 2013, Jeremy Lainé
# All rights reserved.
#
# See AUTHORS file for a full list of contributors.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#     1. Redistributions of source code must retain the above copyright notice,
#        this list of conditions and the following disclaimer.
#
#     2. Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import math
import os
import rrdtool
import simplejson
from copy import copy

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.utils.translation import ugettext_lazy as _

import logging

LOGGER = logging.getLogger(__name__)


def objtype(obj):
    """shorthand for obj.__class__.__name__.lower()"""
    return obj.__class__.__name__.lower()


def sum_metric(metric_parameter, objects, **kwargs):
    """ Get sum of polling values.

    .. warning:: this generate a database request! You have been warned
    """
    metric = Metric.objects.get(parameter=metric_parameter)
    return metric.get_sum_many(objects, **kwargs)


def get_simple_polling(metric_parameter, obj, **kwargs):
    """ Get a single polling value

    .. warning:: this generate a database request! You have been warned
    """
    metric = Metric.objects.get(parameter=metric_parameter)
    return metric.get_polling(obj, **kwargs)


def enrich_object_with_polling(metric_parameter, obj_list, attr=None, **kwargs):
    """ Get polling values, and add it as object attribute

    .. warning:: this generate a database request! You have been warned

    :param metric_parameter: parameter used to get Metric database object
    :type metric_parameter: str
    :param obj_list: list of objects to enrich
    :type obj_list: list
    :param attr: attribute to set (by default, use metric_parameter as attribute)
    :param attr: str
    """
    if attr is None:
        attr = metric_parameter
    metric = Metric.objects.get(parameter=metric_parameter)
    for obj, value in zip(obj_list, metric.get_polling_many(obj_list, **kwargs)):
        setattr(obj, attr, value)


class Graph(models.Model):
    """
    A model representing a graph of a set of monitored metrics.
    """
    TYPE_CHOICES = (
        ('AREA', 'area'),
        ('LINE', 'line'),
    )

    slug = models.SlugField(db_index=True)
    metrics = models.ManyToManyField('Metric')
    title = models.CharField(max_length=255)
    lower_limit = models.IntegerField(blank=True, null=True, default=0)
    upper_limit = models.IntegerField(blank=True, null=True)
    type = models.CharField(choices=TYPE_CHOICES, max_length=255, default='LINE')
    is_stacked = models.BooleanField(default=False)
    is_visible = models.BooleanField(default=True)

    def __unicode__(self):
        return self.title

    class Meta:
        ordering = ['slug']
        verbose_name = _('graph')
        verbose_name_plural = _('graphs')


class MetricManager(models.Manager):
    def get_by_natural_key(self, parameter):
        return self.get(parameter=parameter)


class Metric(models.Model):
    """
    A model representing a monitored metric.
    """
    objects = MetricManager()

    TYPE_CHOICES = (
        ('bool', 'boolean'),
        ('float', 'float'),
        ('int', 'integer'),
        ('str', 'string'),
    )

    name = models.CharField(max_length=32, verbose_name=('name'))
    parameter = models.CharField(max_length=256, verbose_name=('parameter'), db_index=True)
    type = models.CharField(max_length=16, choices=TYPE_CHOICES, default='float')
    unit = models.CharField(max_length=6, blank=True, verbose_name=('unit'))
    rrd_enabled = models.BooleanField(default=True, verbose_name=('RRD enabled'))
    graph_color = models.CharField(blank=True, max_length=8, verbose_name=('graph color'))
    graph_order = models.IntegerField(default=0, verbose_name=('graph order'))
    cache_timeout = models.IntegerField(default=0, verbose_name="Timeout")

    def __init__(self, *args, **kwargs):
        super(Metric, self).__init__(*args, **kwargs)
        self._cache = {}
        self.queue = []
        self.rrd_root = getattr(settings, 'TIMEGRAPH_RRD_ROOT', '/var/lib/rrdcached/db')
        self.cache_prefix = getattr(settings, 'TIMEGRAPH_CACHE_PREFIX', 'timegraph')
        self.heartbeat = getattr(settings, 'TIMEGRAPH_HEARTBEAT', 300)
        self.queue_size = getattr(settings, 'MEMCACHE_QUEUE_SIZE', 3000)

    def natural_key(self):
        return (self.parameter,)

    def get_cached_polling(self, obj):
        """Returns the cached value of the metric for the given object.
        """
        return self._cache[obj.pk]

    def get_cached_polling_list(self, objs):
        """Returns the cached values of the metric for the given objects.
        """
        return [self._cache[obj.pk] for obj in objs]

    def get_polling(self, obj, **kwargs):
        """
        Retrieves the latest value of the metric for the given object.
        """
        try:
            del self._cache[obj.pk]
        except KeyError:
            pass
        return self.get_polling_many((obj,), **kwargs)[0]

    def get_polling_many(self, objs, no_return=False, force_metric_type=True):
        """ Get many cache objects of the same type in one call.

        .. warning:: All objects must have the same class. Only the class
                     of the first object is read.

        You can use no_return=True to add a little optimisation in performances,
        since we will not build a big list of result in that cache (for example,
        if you only want to warn a cache before to use it).

        When force_metric_type is set, we convert None values (i.e not in the
        cache) to 0, 0.0 or False (it depends on the metric type).
        When it's not set, we return None for not found values.
        """
        if len(objs) == 0:
            return []
        pre_key = self._pre_key_for(*objs)

        # Build a list of all object keys
        keys = []
        for obj in objs:
            pk = obj.pk
            if pk not in self._cache:
                keys.append((pre_key % str(pk).replace(':', ''), pk))

        # fetch them from the cache
        if len(keys) > 0:
            metrics = cache.get_many([key for key, _ in keys], raw=True)
            # Fill the cache with the massive result
            for key, pk in keys:
                if key in metrics:
                    self._cache[pk] = self.to_python(metrics[key],
                                                     force_metric_type=force_metric_type)
                else:
                    self._cache[pk] = None

        if no_return:
            return None

        return self.get_cached_polling_list(objs)

    def get_sum_many(self, objs, **kwargs):
        return sum((x for x in self.get_polling_many(objs, **kwargs) if x is not None))

    def set_polling(self, obj, value):
        """Stores the latest value of the metric for the given object.
        """
        self.set_polling_many(((obj, value),))

    def set_polling_many(self, objs_and_values):
        """ Set several cache values in one call.
        """
        cache_dict = {}
        if len(objs_and_values) == 0:
            return
        first_obj = objs_and_values[0][0]
        pre_key = self._pre_key_for(first_obj)
        pre_path = os.path.join(self.rrd_root, objtype(first_obj))
        filename = '%s.rrd' % self.pk

        for obj, value in objs_and_values:
            obj_pk = str(obj.pk).replace(':', '')
            key = pre_key % obj_pk
            cache_dict[key] = value
            if self.rrd_enabled and value not in (None, ''):
                filepath = os.path.join(pre_path, obj_pk, filename)
                try:
                    # we could use os.path.exists here, but python calls stat()
                    # to check and that is too slow for our use case.
                    # Use an exception for such rare cases
                    rrdtool.update(filepath, "N:{}".format(value))
                except rrdtool.error as err:
                    if "No such file" not in err.message:
                        LOGGER.error("error on %s metric (object %s): %s", self.parameter,
                                     obj_pk, err)
                        continue

                    dirpath = os.path.dirname(filepath)
                    if not os.path.exists(dirpath):
                        os.makedirs(dirpath)
                    rrdtool.create(filepath,
                                   'DS:%s:GAUGE:%s:U:U' % (self.id, self.heartbeat),
                                   'RRA:AVERAGE:0.5:1:600',
                                   'RRA:AVERAGE:0.5:6:600',
                                   'RRA:AVERAGE:0.5:24:600',
                                   'RRA:AVERAGE:0.5:288:600')  # Up to 600d
                    # As rrdupdate manpage says, "using the letter 'N', in which
                    # case the update time is set to be the current time
                    rrdtool.update(filepath, "N:{}".format(value))
        timeout = self.cache_timeout if self.cache_timeout else 7 * 86400
        cache.set_many(cache_dict, timeout=timeout, raw=True)

    def dump_queue(self):
        """Flushes the inner object queue, previously filled with
        queue_append(), to cache server.
        """
        self.set_polling_many(self.queue)
        self.queue = []

    def queue_append(self, obj, value):
        """ The max queue size is not so easy to explain here.
        The current python memcache backend does the following on
        the memcached socket:
            1) sendall()
            2) read with recv()
        Reading the response begins only when all data are sent. But in the
        same time, memcached is replying as soon as possible.
        The read buffer can then be full. Memcache does not accept input
        anymore. The socket is dead.

        To prevent this, the solution is to increase net.core.wmem_default
        in the sysctl. Memcached can then fill a big buffer, readed at the
        end by python. To get the maximum value on our system (and adapt the
        sysctl when needed), you can do:

        import memcache
        PREFIX = "toto"
        SOCK_PATH = "unix:/tmp/memcached-1.socket"

        def flush(mydict):
            sock = memcache.Client([SOCK_PATH], debug=1, socket_timeout=5)
            return sock.set_multi(mydict, 600)

        def add_entries(mydict, begin, end):
            for x in xrange(begin, end):
                mydict["%s-%s" % (PREFIX, x)] = 1

        def main():
            size = 0
            mydict = {}
            while True:
                add_entries(mydict, size, size + 1000)
                if len(flush(mydict)) > 0:  # flush return the list of failing entries
                    break
                size += 1000
            print "Max size is %s" % (size - 1000)

        main()

        TODO: check behaviour with redis
        """
        self.queue.append((obj, value))
        if len(self.queue) > self.queue_size:
            self.dump_queue()

    @property
    def is_summable(self):
        """
        True if the metric is summable, False otherwise.
        """
        return (self.type in ['float', 'int']) and (self.unit not in [u'%', u'°', u'°C', u'°F'])

    def to_python(self, value, force_metric_type=True):
        """
        Converts the given string value to a python value.
        """
        if self.type == 'float':
            try:
                return float(value)
            except (ValueError, TypeError):
                if force_metric_type:
                    return 0.0
                return None
        elif self.type == 'int':
            try:
                return int(value)
            except (ValueError, TypeError):
                if force_metric_type:
                    return 0
                return None
        elif self.type == 'bool':
            if value in ['1', 'True']:
                return True
            elif not force_metric_type and value is None:
                return None
            return False
        else:
            return value and unicode(value) or ''

    def xport(self, args, object_list, op="+", un_value="0", json=False):
        key = self.parameter
        xvars = []
        args = copy(args)
        for obj in object_list:
            rrd_path = self._rrd_path(obj)
            if os.path.exists(rrd_path):
                xkey = '%s%i' % (key, obj.pk)
                args += ['DEF:%s=%s:%s:AVERAGE' %
                         (xkey, rrd_path, self.pk)]
                xvars += [xkey]

        if not xvars:
            return None

        # replace the unknown values with un_value,
        # BTW 'ADDNAN' version of '+' operator does not need that
        # but other ops do.
        # HP41 memories...
        variables = xvars
        # xvars = [ [var, "UN", un_value, var, "IF"] for var in xvars ]
        xvars = [
            [var, "UN", "0.0", var, "IF", "0.0", "EQ", un_value, var, "IF"]
            for var in xvars]
        xvars = [item for sublist in xvars for item in sublist]

        xvars += [op for x in variables[1:]]
        args += ['CDEF:%s=%s' % (key, ','.join(xvars))]
        args += ['XPORT:{0}:{0}'.format(key)]

        args = map(str, args)
        output = rrdtool.xport(*args)

        if not output['data']:
            return None

        # we cheat , if the last value is 0 we copy the n-1 value in it
        # and if first value is 0 too we do the same. The goal is to have
        # a relatively smoothed line to display.
        if output['data'][-1] == 0.0:
            output['data'][-1] = output[key][-2]

        if output['data'][0] == 0.0:
            output['data'][0] = output[key][1]

        if json:
            return simplejson.dumps(output)

        return output

    def _rrd_path(self, obj):
        """
        RRD path for the given object.
        """
        obj_type = objtype(obj)
        obj_pk = str(obj.pk).replace(':', '')
        return os.path.join(self.rrd_root, obj_type, obj_pk, '%s.rrd' % self.pk)

    def _pre_key_for(self, *objects):
        """Returns the pre-key for the given object(s).

        .. warning:: All objects must have the same class. Only the class
             of the first object is read.
        """
        obj_type = objtype(objects[0])
        return "{}/{}/%s/{}".format(self.cache_prefix, obj_type, str(self.pk))

    def __unicode__(self):
        return self.name

    class Meta:
        verbose_name = _('metric')
        verbose_name_plural = _('metrics')


def format_with_prefix(value, unit):
    """
    Formats a float value with the appropriate SI prefix.
    """
    base = 1000.0
    prefixes = ['', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y', 'y', 'z', 'a', 'f', 'p', 'n', u'µ', 'm']
    if unit in ['b', 'B']:
        base = 1024.0
        for i, prefix in enumerate(prefixes):
            if prefix:
                prefixes[i] = prefix + 'i'
    l = value and max(-8, min(math.log(abs(value)) / math.log(base), 8)) or 0
    if l >= 0:
        l = int(l)
    else:
        l = int(math.floor(l))
    return u'%.1f %s%s' % (value / (base ** l), prefixes[l], unit)


def format_value(value, unit):
    """
    Formats the given value with the specified unit.
    """

    units_raw_format = (u'%', u'°', u'°C', u'°F', u'dBm')

    if value in [None, '']:
        return ''

    if isinstance(value, (bool, str, unicode)):
        if unit:
            return '%s %s' % (value, unit)
        else:
            return value

    if isinstance(value, float):
        if unit in units_raw_format:
            return '%.1f %s' % (value, unit)

        return format_with_prefix(value, unit)

    if isinstance(value, (int, long)):
        if unit in units_raw_format:
            return '%i %s' % (value, unit)
        if value < 1000:
            if unit:
                return '%i %s' % (value, unit)
            else:
                return '%i' % value

        return format_with_prefix(value, unit)

    return ''

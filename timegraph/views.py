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

import os
import subprocess
import rrdtool
import tempfile
import xml.etree.ElementTree as ET
import simplejson

from django.http import HttpResponse, HttpResponseBadRequest, Http404
from django.utils.encoding import force_unicode

from timegraph.forms import GraphForm
from timegraph.models import format_value

# colors from munin
COLORS = [
    '#00CC00', '#0066B3', '#FF8000', '#FFCC00', '#330099', '#990099', '#CCFF00', '#FF0000', '#808080',
    '#008F00', '#00487D', '#B35A00', '#B38F00', '#6B006B', '#8FB300', '#B30000', '#BEBEBE',
    '#80FF80', '#80C9FF', '#FFC080', '#FFE680', '#AA80FF', '#EE00CC', '#FF8080',
    '#666600', '#FFBFFF', '#00FFCC', '#CC6699', '#999900',
]

def render_graph(request, graph, obj):
    """
    Renders the specified graph.
    """
    # validate input
    form = GraphForm(request.GET)
    if not form.is_valid():
        return HttpResponseBadRequest()

    count = 0
    options = []
    if graph.is_stacked:
        stack = ':STACK'
    else:
        stack = ''
    is_memory = False
    for metric in graph.metrics.order_by('graph_order'):
        if metric.unit in ['b', 'B']:
            is_memory = True
        data_file = metric._rrd_path(obj)
        value = metric.get_polling(obj)
        if os.path.exists(data_file):
            color = metric.graph_color
            if not color:
                color = COLORS[count % len(COLORS)]

            # current value
            value_str = format_value(value, metric.unit)
            if value_str:
                value_str = ' | ' + value_str

            options += [
                'DEF:%s=%s:%s:AVERAGE' % (count, data_file, metric.pk),
                '%s:%s%s:%s%s%s' % (graph.type, count, color, metric.name, value_str, stack)]
            count += 1

    # if no RRDs were found stop here
    if not count:
        raise Http404

    if is_memory:
        options += ['--base', '1024']
    if graph.lower_limit is not None:
        options += [ '--lower-limit', str(graph.lower_limit) ]
        options += [ '-r' ]
    if graph.upper_limit is not None:
        options += [ '--upper-limit', str(graph.upper_limit) ]
    options += form.options()
    image_data = timegraph_rrd(options)

    return HttpResponse(image_data, content_type='image/png')

def render_metric(request, metric, object_list):
    """
    Renders the total for the given metric.
    """
    # validate input
    form = GraphForm(request.GET)
    if not form.is_valid():
        return HttpResponseBadRequest()

    color = metric.graph_color
    if not color:
        color = '#990033'

    count = 0
    options = []
    type = 'AREA'
    for obj in object_list:
        data_file = metric._rrd_path(obj)
        if os.path.exists(data_file):
            options += [
                'DEF:%s=%s:%s:AVERAGE' % (count, data_file, metric.pk),
                '%s:%s%s' % (type, count, color)]
            type = 'STACK'
            count += 1

    # if no RRDs were found stop here
    if not count:
        raise Http404

    options += form.options()
    image_data = timegraph_rrd(options)

    return HttpResponse(image_data, content_type='image/png')

def timegraph_rrd(options):
    """
    Invokes rrd_graph with the given options and returns the image data.
    """
    image_file = tempfile.NamedTemporaryFile()
    rrdtool.graph([str(image_file.name)] + [ force_unicode(x).encode('utf-8') for x in options ])
    return image_file.read()

#
# Data extraction and reporting routines
#

def _rrd_export_wrap(args, object_list, exports, op="+", un_value="0",
                     json=True):
    """ Export RRD data as JSON for a set of objects and their metrics.

    Where 'args' is a list of additional parameters to be passed to rrdtool
    like start date, and step..., each item is the name or the value of a
    parameter, 'object_list' is a list of kiwi objects and 'exports' is a
    dictionnary of the metrics we want to export with their labels.
    If there is more than one objects their metric values will be aggregated
    by the operation defined in 'op', for example '+' will create a vector
    of the sums of the bandwidth of the lines and MAX will create a vector
    of the MAX values.
    un_value replace the unknown values retrieved from the RRDs.
    Take a look at the rrdtool documentation to learn which RPN operations
    may be used.
    """
    keys = []
    for key, metric in exports.iteritems():
        xvars = []
        for obj in object_list:
            rrd_path = metric._rrd_path(obj)
            if os.path.exists(rrd_path):
                xkey = '%s%i' % (key, obj.pk)
                args += ['DEF:%s=%s:%s:AVERAGE' %
                         (xkey, metric._rrd_path(obj), metric.pk)]
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
        args += ['XPORT:%s' % key]
        keys += [key]

    p = subprocess.Popen(['rrdtool', 'xport'] + args, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    out, err = p.communicate()
    if p.poll():
        raise Exception("rrdtool xport failed %s" % err)

    #  transform the XML data into a dictionnary composed with a
    # list of 'stamp' dates and some lists of values, each list key
    # being the label of the metric
    output = {'stamp': []}
    for key in keys:
        output[key] = []

    tree = ET.fromstring(out)
    for row in tree.findall('data/row'):
        output['stamp'].append(int(row.find('t').text))
        values = row.findall('v')
        for counter, key in enumerate(keys):
            value = values[counter].text
            if value in ('NaN', 'nan'):
                output[key] += [None]
            else:
                output[key] += [float(value)]

    # we cheat , if the last value is 0 we copy the n-1 value in it
    # and if first value is 0 too we do the same. The goal is to have
    # a relatively smoothed line to display.
    for key in keys:
        if output[key][-1] == 0.0:
            output[key][-1] = output[key][-2]

        if output[key][0] == 0.0:
            output[key][0] = output[key][1]

        # FIXME: replace the 9999 pings values with '' values or something else

    if json:
        return simplejson.dumps(output)
    else:
        return output

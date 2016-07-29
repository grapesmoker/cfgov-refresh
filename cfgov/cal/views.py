import sys
import os
import six
import urllib2
from httplib import BadStatusLine

from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

from rest_framework import generics

from django.shortcuts import render, render_to_response
from django.http import HttpResponse, HttpResponseRedirect
from django import forms
from django.utils.safestring import mark_safe
from django.template import RequestContext
from django.template.loader import get_template
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Max, Min, Q
from django.contrib import messages

from cal.models import CFPBCalendar, CFPBCalendarEvent
from v1.util.util import ERROR_MESSAGES
from sheerlike.templates import get_date_string

## TODO: Update to python 3 when PDFreactor's python wrapper supports it.
if six.PY2:
    try:
        sys.path.append(os.environ.get('PDFREACTOR_LIB'))
        from PDFreactor import *
    except ImportError:
       PDFreactor = None


class FilterErrorList(forms.utils.ErrorList):
    def __str__(self):
        return '\n'.join(str(e) for e in self)

class FilterCheckboxList(forms.MultipleChoiceField):
    def validate(self, value):
        if self.required and not value:
            msg = self.error_messages['required']
            if self.label and '%s' in msg:
                msg = msg % self.label
            raise forms.ValidationError(msg, code='required')
        return value

class FilterDateField(forms.DateField):
    def clean(self, value):
        if value:
            try:
                value = get_date_string(value)
            except ValueError:
                msg = self.error_messages['invalid']
                if '%s'in msg:
                    msg = msg % value
                raise forms.ValidationError( msg , code='invalid')
        return value

class CalendarFilterForm(forms.Form):
    filter_calendar = forms.MultipleChoiceField(
            choices = [(c.title, c.title) for c in CFPBCalendar.objects.all()],
            required=False)
    filter_range_date_gte = forms.DateField(required=False)
    filter_range_date_lte = forms.DateField(required=False)

    def clean_filter_calendar(self):
        calendar_names = self.cleaned_data['filter_calendar']
        calendars = CFPBCalendar.objects.filter(title__in=calendar_names)
        return calendars

class CalendarPDFForm(forms.Form):
    filter_calendar = FilterCheckboxList(
            choices = [(c.title, c.title) for c in CFPBCalendar.objects.all()],
            required=True,
            label='Calendar',
            error_messages=ERROR_MESSAGES['CHECKBOX_ERRORS'])
    filter_range_date_gte = FilterDateField(required=False,
        error_messages=ERROR_MESSAGES['DATE_ERRORS'])
    filter_range_date_lte = FilterDateField(required=False,
        error_messages=ERROR_MESSAGES['DATE_ERRORS'])

    def __init__(self, *args, **kwargs):
        kwargs['error_class'] = FilterErrorList
        super(CalendarPDFForm, self).__init__(*args, **kwargs)

    def clean_filter_calendar(self):
        calendar_names = self.cleaned_data['filter_calendar']
        calendars = CFPBCalendar.objects.filter(title__in=calendar_names)
        return calendars

    def clean(self):
        cleaned_data = super(CalendarPDFForm, self).clean()
        from_date_empty = 'filter_range_date_gte' in cleaned_data and \
                          cleaned_data['filter_range_date_gte']  == None
        to_date_empty = 'filter_range_date_lte' in cleaned_data and \
                        cleaned_data['filter_range_date_lte'] == None

        if from_date_empty and to_date_empty :
            raise forms.ValidationError(ERROR_MESSAGES['DATE_ERRORS']['one_required'])
        return cleaned_data

class PaginatorForSheerTemplates(Paginator):
    def __init__(self, request, *args, **kwargs):
        self.request = request
        super(PaginatorForSheerTemplates, self).__init__(*args, **kwargs)

    @property
    def pages(self):
        return self.num_pages

    def url_for_page(self,pagenum):
        url_args = self.request.GET.copy()
        url_args['page'] = pagenum
        return self.request.path +'?' + url_args.urlencode()


def display(request, pdf=False):
    """
    display (potentially filtered) html view of the calendar
    """

    template_name='about-us/the-bureau/leadership-calendar/index.html'
    form = CalendarPDFForm(request.GET) if pdf else CalendarFilterForm(request.GET)
    context = {'form': form}
    form_is_vaild = form.is_valid()

    if form_is_vaild:
        cal_q = Q(('active', True))
        if form.cleaned_data.get('filter_calendar', None):
            calendars = form.cleaned_data['filter_calendar']
            cal_q &= Q(('calendar__in', calendars))

        if form.cleaned_data.get('filter_range_date_gte'):
            gte = form.cleaned_data['filter_range_date_gte']
            cal_q &= Q(('dtstart__gte', gte))

        if form.cleaned_data.get('filter_range_date_lte'):
            # adding timedelta(days=1) makes the end of the range inclusive
            lte = form.cleaned_data['filter_range_date_lte']+ timedelta(days=1)
            cal_q &= Q(('dtend__lte', lte))


        events = CFPBCalendarEvent.objects.filter(cal_q).order_by('-dtstart')
        datetimes = events.datetimes('dtstart', 'day', order='DESC')
        paginator = PaginatorForSheerTemplates(request, datetimes, 5)
        page_num = int(request.GET.get('page', 1))

        try:
            page_days = paginator.page(page_num)
            paginator.current_page = page_num
        except PageNotAnInteger:
            # If page is not an integer, deliver first page.
            page_days = paginator.page(1)
            paginator.current_page = 1
        except EmptyPage:
            # If page is out of range (e.g. 9999), deliver last page of results.
            page_days = paginator.page(paginator.num_pages)
            paginator.current_page = paginator.num_pages

        stats = events.aggregate(Min('dtstart'), Max('dtend'))
        range_start = form.cleaned_data.get('filter_range_date_gte') or stats['dtstart__min']
        range_end = form.cleaned_data.get('filter_range_date_lte') or stats['dtend__max']

        # The first date in the list needs to include all the events for that day
        object_list = list(page_days.object_list)
        if len(object_list) == 1:
            day_range = [object_list[0] + timedelta(hours=23, minutes=59, seconds=59),
                         object_list[0]]
            context.update({'day_range': day_range})
        elif object_list:
            object_list[0] += timedelta(hours=23, minutes=59, seconds=59)
            page_days.object_list = object_list

        context.update({
            'paginator': paginator,
            'events': events,
            'page_days': page_days,
            'range_start': range_start,
            'range_end':range_end,
        })

        if pdf:
            template_name = 'about-us/the-bureau/leadership-calendar/print/index.html'
            if PDFreactor:
                return pdf_response(request, context)

    elif form_is_vaild == False and pdf:
        for error in form.errors.itervalues():
            messages.error(request, str(error), extra_tags='leadership-calendar')

        return HttpResponseRedirect('/the-bureau/leadership-calendar/')

    return render(request, template_name, context)

def pdf_response(request, context):
    template_name = 'about-us/the-bureau/leadership-calendar/print/index.html'
    license = os.environ.get('PDFREACTOR_LICENSE')
    stylesheet_url = '/static/css/pdfreactor-fonts.css'
    pdf_reactor = PDFreactor()

    pdf_reactor.setBaseURL("%s://%s/" % (request.scheme, request.get_host()))
    pdf_reactor.setLogLevel(PDFreactor.LOG_LEVEL_WARN)
    pdf_reactor.setLicenseKey(str(license))
    pdf_reactor.setAuthor('CFPB')
    pdf_reactor.setAddTags(True)
    pdf_reactor.setAddBookmarks(True)
    pdf_reactor.addUserStyleSheet('', '', '', stylesheet_url)

    template = get_template(template_name)
    html = template.render(context)
    html = html.replace(u"\u2018", "'").replace(u"\u2019", "'")
    try:
        pdf = pdf_reactor.renderDocumentFromContent(html.encode('utf-8'))
        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename=cfpb-leadership.pdf'
        return response
    except (urllib2.HTTPError, urllib2.URLError, BadStatusLine):
        messages.error(request, 'Error Creating PDF', extra_tags='leadership-calendar')
        return HttpResponseRedirect('/the-bureau/leadership-calendar/')

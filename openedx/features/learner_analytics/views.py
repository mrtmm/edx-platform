"""
Learner analytics dashboard views
"""
import logging
import math
import urllib
from datetime import datetime, timedelta

import pytz
import requests
from analyticsclient.client import Client
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.http import Http404
from django.shortcuts import render_to_response
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_control
from django.views.generic import View
from opaque_keys.edx.keys import CourseKey
from student.models import CourseEnrollment
from util.views import ensure_valid_course_key
from xmodule.modulestore.django import modulestore

from lms.djangoapps.course_api.blocks.api import get_blocks
from lms.djangoapps.courseware.courses import get_course_with_access
from lms.djangoapps.discussion.views import create_user_profile_context
from lms.djangoapps.grades.course_grade_factory import CourseGradeFactory
from openedx.features.course_experience import default_course_url_name
from openedx.core.djangoapps.user_api.accounts.image_helpers import get_profile_image_urls_for_user

from . import ENABLE_DASHBOARD_TAB

log = logging.getLogger(__name__)


class LearnerAnalyticsView(View):
    """
    Displays the Learner Analytics Dashboard.
    """
    def __init__(self):
        View.__init__(self)
        self.analytics_client = Client(base_url=settings.ANALYTICS_API_URL, auth_token=settings.ANALYTICS_API_KEY)

    @method_decorator(login_required)
    @method_decorator(cache_control(no_cache=True, no_store=True, must_revalidate=True))
    @method_decorator(ensure_valid_course_key)
    def get(self, request, course_id):
        """
        Displays the user's Learner Analytics for the specified course.

        Arguments:
            request: HTTP request
            course_id (unicode): course id
        """
        course_key = CourseKey.from_string(course_id)
        if not ENABLE_DASHBOARD_TAB.is_enabled(course_key):
            raise Http404

        course = get_course_with_access(request.user, 'load', course_key, check_if_enrolled=True)
        course_url_name = default_course_url_name(course.id)
        course_url = reverse(course_url_name, kwargs={'course_id': unicode(course.id)})

        grading_policy = course.grading_policy

        (grade_data, answered_percent) = self.get_grade_data(request.user, course_key, grading_policy['GRADE_CUTOFFS'])
        schedule_data = self.get_assignments_with_due_date(request, course_key)
        (grade_data, schedule_data) = self.sort_grade_and_schedule_data(grade_data, schedule_data)

        context = {
            'course': course,
            'course_url': course_url,
            'disable_courseware_js': True,
            'uses_pattern_library': True,
            'is_self_paced': course.self_paced,
            'is_verified': CourseEnrollment.is_enrolled_as_verified(request.user, course_key),
            'grading_policy': grading_policy,
            'assignment_grades': grade_data,
            'answered_percent': answered_percent,
            'assignment_schedule': schedule_data,
            'profile_image_urls': get_profile_image_urls_for_user(request.user, request),
            'discussion_info': self.get_discussion_data(request, course_key),
            'weekly_active_users': self.get_weekly_course_activity_count(course_key),
            'week_streak': self.consecutive_weeks_of_course_activity_for_user(
                request.user.username, course_key
            )
        }

        return render_to_response('learner_analytics/dashboard.html', context)

    def get_grade_data(self, user, course_key, grade_cutoffs):
        """
        Collects and formats the grades data for a particular user and course.

        Args:
            user (User)
            course_key (CourseKey)
            grade_cutoffs: # TODO: LEARNER-3854: Complete docstring if implementing Learner Analytics.
        """
        course_grade = CourseGradeFactory().read(user, course_key=course_key)
        grades = []
        total_earned = 0
        total_possible = 0
        answered_percent = None
        for (location, subsection_grade) in course_grade.subsection_grades.iteritems():
            if subsection_grade.format is not None:
                possible = subsection_grade.graded_total.possible
                earned = subsection_grade.graded_total.earned
                passing_grade = math.ceil(possible * grade_cutoffs['Pass'])
                grades.append({
                    'assignment_type': subsection_grade.format,
                    'total_earned': earned,
                    'total_possible': possible,
                    'passing_grade': passing_grade,
                    'location': unicode(location),
                    'assigment_url': reverse('jump_to_id', kwargs={
                        'course_id': unicode(course_key),
                        'module_id': unicode(location),
                    })
                })
                if earned > 0:
                    total_earned += earned
                    total_possible += possible

        if total_possible > 0:
            answered_percent = float(total_earned) / total_possible
        return (grades, answered_percent)

    def sort_grade_and_schedule_data(self, grade_data, schedule_data):
        """
        Sort the assignments in grade_data and schedule_data to be in the same order.
        """
        schedule_dict = {assignment['location']: assignment for assignment in schedule_data}

        sorted_schedule_data = []
        sorted_grade_data = []
        for grade in grade_data:
            assignment = schedule_dict.get(grade['location'])
            if assignment:
                sorted_grade_data.append(grade)
                sorted_schedule_data.append(assignment)

        return sorted_grade_data, sorted_schedule_data

    def get_discussion_data(self, request, course_key):
        """
        Collects and formats the discussion data from a particular user and course.

        Args:
            request (HttpRequest)
            course_key (CourseKey)
        """
        context = create_user_profile_context(request, course_key, request.user.id)
        threads = context['threads']
        profiled_user = context['profiled_user']

        # TODO: LEARNER-3854: If implementing Learner Analytics, rename to content_authored_count.
        content_authored = profiled_user['threads_count'] + profiled_user['comments_count']
        thread_votes = 0
        for thread in threads:
            if thread['user_id'] == profiled_user['external_id']:
                thread_votes += thread['votes']['count']
        discussion_data = {
            'content_authored': content_authored,
            'thread_votes': thread_votes,
        }
        return discussion_data

    def get_assignments_with_due_date(self, request, course_key):
        """
        Returns a list of assignment (graded) blocks with due dates, including
        due date and location.

        Args:
            request (HttpRequest)
            course_key (CourseKey)
        """
        course_usage_key = modulestore().make_course_usage_key(course_key)
        all_blocks = get_blocks(
            request,
            course_usage_key,
            user=request.user,
            nav_depth=3,
            requested_fields=['display_name', 'due', 'graded', 'format'],
            block_types_filter=['sequential']
        )
        assignment_blocks = []
        for (location, block) in all_blocks['blocks'].iteritems():
            if block.get('graded', False) and block.get('due') is not None:
                assignment_blocks.append(block)
                block['due'] = block['due'].isoformat()
                block['location'] = unicode(location)

        return assignment_blocks

    def get_weekly_course_activity_count(self, course_key):
        """
        Get the count of any course activity (total for all users) from previous 7 days.

        Args:
            course_key (CourseKey)
        """
        cache_key = 'learner_analytics_{course_key}_weekly_activities'.format(course_key=course_key)
        activities = cache.get(cache_key)

        if not activities:
            log.info('Weekly course activities for course {course_key} was not cached - fetching from Analytics API'
                     .format(course_key=course_key))
            weekly_course_activities = self.analytics_client.courses(course_key).activity()

            if not weekly_course_activities or 'any' not in weekly_course_activities[0]:
                return 0

            # weekly course activities should only have one item
            activities = weekly_course_activities[0]
            cache.set(cache_key, activities, LearnerAnalyticsView.seconds_to_cache_expiration())

        return activities['any']

    def consecutive_weeks_of_course_activity_for_user(self, username, course_key):
        """
        Get the most recent count of consecutive days that a user has performed a course activity

        Args:
            username (str)
            course_key (CourseKey)
        """
        cache_key = 'learner_analytics_{username}_{course_key}_engagement_timeline'\
            .format(username=username, course_key=course_key)
        timeline = cache.get(cache_key)

        if not timeline:
            log.info('Engagement timeline for course {course_key} was not cached - fetching from Analytics API'
                     .format(course_key=course_key))

            # TODO (LEARNER-3470): @jaebradley replace this once the Analytics client has an engagement timeline method
            url = '{base_url}/engagement_timelines/{username}?course_id={course_key}'\
                .format(base_url=settings.ANALYTICS_API_URL,
                        username=username,
                        course_key=urllib.quote_plus(unicode(course_key)))
            headers = {'Authorization': 'Token {token}'.format(token=settings.ANALYTICS_API_KEY)}
            response = requests.get(url=url, headers=headers)
            data = response.json()

            if not data or 'days' not in data or not data['days']:
                return 0

            # Analytics API returns data in ascending (by date) order - we want to count starting from most recent day
            data_ordered_by_date_descending = list(reversed(data['days']))

            cache.set(cache_key, data_ordered_by_date_descending, LearnerAnalyticsView.seconds_to_cache_expiration())
            timeline = data_ordered_by_date_descending

        return LearnerAnalyticsView.calculate_week_streak(timeline)

    @staticmethod
    def calculate_week_streak(daily_activities):
        """
        Check number of weeks in a row that a user has performed some activity.

        Regardless of when a week starts, a sufficient condition for checking if a specific week had any user activity
        (given a list of daily activities ordered by date) is to iterate through the list of days 7 days at a time and
        check to see if any of those days had any activity.

        Args:
            daily_activities: sorted list of dictionaries containing activities and their counts
        """
        week_streak = 0
        seven_day_buckets = [daily_activities[i:i + 7] for i in range(0, len(daily_activities), 7)]
        for bucket in seven_day_buckets:
            if any(LearnerAnalyticsView.has_activity(day) for day in bucket):
                week_streak += 1
            else:
                return week_streak
        return week_streak

    @staticmethod
    def has_activity(daily_activity):
        """
        Validate that a course had some activity that day

        Args:
            daily_activity: dictionary of activities and their counts
        """
        return int(daily_activity['problems_attempted']) > 0 \
            or int(daily_activity['problems_completed']) > 0 \
            or int(daily_activity['discussion_contributions']) > 0 \
            or int(daily_activity['videos_viewed']) > 0

    @staticmethod
    def seconds_to_cache_expiration():
        """Calculate cache expiration seconds. Currently set to seconds until midnight UTC"""
        next_midnight_utc = (datetime.today() + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                                           microsecond=0, tzinfo=pytz.utc)
        now_utc = datetime.now(tz=pytz.utc)
        return round((next_midnight_utc - now_utc).total_seconds())

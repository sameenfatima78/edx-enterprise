# -*- coding: utf-8 -*-
"""
Assist integrated channels with retrieving learner completion data.

Module contains resources for integrated pipelines to retrieve all the
grade and completion data for enrollments belonging to a particular
enterprise customer.
"""

from __future__ import absolute_import, unicode_literals

import json
from logging import getLogger

from slumber.exceptions import HttpNotFoundError

from django.apps import apps
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from consent.models import DataSharingConsent
from enterprise.api_client.lms import CertificatesApiClient, CourseApiClient, GradesApiClient
from enterprise.models import EnterpriseCourseEnrollment
from integrated_channels.integrated_channel.exporters import Exporter
from integrated_channels.utils import parse_datetime_to_epoch_millis

LOGGER = getLogger(__name__)


class LearnerExporter(Exporter):
    """
    Base class for exporting learner completion data to integrated channels.
    """

    GRADE_PASSING = 'Pass'
    GRADE_FAILING = 'Fail'
    GRADE_INCOMPLETE = 'In Progress'

    def __init__(self, user, enterprise_configuration):
        """
        Store the data needed to export the learner data to the integrated channel.

        Arguments:

        * ``user``: User instance with access to the Grades API for the Enterprise Customer's courses.
        * ``enterprise_configuration``: EnterpriseCustomerPluginConfiguration instance for the current channel.
        """
        # The Grades API and Certificates API clients require an OAuth2 access token,
        # so cache the client to allow the token to be reused. Cache other clients for
        # general reuse.
        self.grades_api = None
        self.certificates_api = None
        self.course_api = None
        self.course_enrollment_api = None
        super(LearnerExporter, self).__init__(user, enterprise_configuration)

    @property
    def grade_passing(self):
        """
        Returns the string used for a passing grade.
        """
        return self.GRADE_PASSING

    @property
    def grade_failing(self):
        """
        Returns the string used for a failing grade.
        """
        return self.GRADE_FAILING

    @property
    def grade_incomplete(self):
        """
        Returns the string used for an incomplete course grade.
        """
        return self.GRADE_INCOMPLETE

    def export(self, **kwargs):
        """
        Collect learner data for the ``EnterpriseCustomer`` where data sharing consent is granted.

        Yields a learner data object for each enrollment, containing:

        * ``enterprise_enrollment``: ``EnterpriseCourseEnrollment`` object.
        * ``completed_date``: datetime instance containing the course/enrollment completion date; None if not complete.
          "Course completion" occurs for instructor-paced courses when course certificates are issued, and
          for self-paced courses, when the course end date is passed, or when the learner achieves a passing grade.
        * ``grade``: string grade recorded for the learner in the course.
        """
        exporting_single_learner = False
        learner_to_transmit = kwargs.get('learner_to_transmit', None)
        course_run_id = kwargs.get('course_run_id', None)
        completed_date = kwargs.get('completed_date', None)
        is_passing = kwargs.get('is_passing', False)
        grade = kwargs.get('grade', None)
        skip_transmitted = kwargs.get('skip_transmitted', True)
        TransmissionAudit = kwargs.get('TransmissionAudit', None)  # pylint: disable=invalid-name
        # Fetch the consenting enrollment data, including the enterprise_customer_user.
        # Order by the course_id, to avoid fetching course API data more than we have to.
        LOGGER.info('[Integrated Channel] Starting Export.'
                    ' CompletedDate: {completed_date}, Course: {course_run}, Grade: {grade}, IsPassing: {is_passing},'
                    ' User: {user_id}'.format(
                        completed_date=completed_date,
                        course_run=course_run_id,
                        grade=grade,
                        is_passing=is_passing,
                        user_id=learner_to_transmit.id if learner_to_transmit else None))
        enrollment_queryset = EnterpriseCourseEnrollment.objects.select_related(
            'enterprise_customer_user'
        ).filter(
            enterprise_customer_user__enterprise_customer=self.enterprise_customer,
            enterprise_customer_user__active=True,
        )
        if learner_to_transmit and course_run_id:
            enrollment_queryset = enrollment_queryset.filter(
                course_id=course_run_id,
                enterprise_customer_user__user_id=learner_to_transmit.id,
            )
            exporting_single_learner = True
            LOGGER.info('[Integrated Channel] Exporting single learner.'
                        ' Course: {course_run}, Enterprise: {enterprise_slug}, User: {user_id}'.format(
                            course_run=course_run_id,
                            enterprise_slug=self.enterprise_customer.slug,
                            user_id=learner_to_transmit.id))
        enrollment_queryset = enrollment_queryset.order_by('course_id')

        # Fetch course details from the Course API, and cache between calls.
        course_details = None
        for enterprise_enrollment in enrollment_queryset:

            if TransmissionAudit and skip_transmitted:
                already_transmitted = TransmissionAudit.objects.filter(
                    enterprise_course_enrollment_id=enterprise_enrollment.id,
                    error_message=''
                )
                if already_transmitted.exists():
                    # We've already sent a completion status call for this enrollment
                    LOGGER.info('[Integrated Channel] Skipping export of previously sent enterprise enrollment.'
                                '  EnterpriseEnrollment: {enterprise_enrollment_id}'.format(
                                    enterprise_enrollment_id=enterprise_enrollment.id))
                    continue

            course_id = enterprise_enrollment.course_id

            # Fetch course details from Courses API
            # pylint: disable=unsubscriptable-object
            if course_details is None or course_details['course_id'] != course_id:
                if self.course_api is None:
                    self.course_api = CourseApiClient()
                course_details = self.course_api.get_course_details(course_id)

            if course_details is None:
                # Course not found, so we have nothing to report.
                LOGGER.error('[Integrated Channel] Course run details not found.'
                             ' EnterpriseEnrollment: {enterprise_enrollment_pk}, Course: {course_id}'.format(
                                 enterprise_enrollment_pk=enterprise_enrollment.pk,
                                 course_id=course_id))
                continue

            consent = DataSharingConsent.objects.proxied_get(
                username=enterprise_enrollment.enterprise_customer_user.username,
                course_id=enterprise_enrollment.course_id,
                enterprise_customer=enterprise_enrollment.enterprise_customer_user.enterprise_customer
            )

            if not consent.granted or enterprise_enrollment.audit_reporting_disabled:
                continue

            # For instructor-paced courses, let the certificate determine course completion
            if course_details.get('pacing') == 'instructor':
                completed_date_from_api, grade_from_api, is_passing_from_api = \
                    self._collect_certificate_data(enterprise_enrollment)
                LOGGER.info('[Integrated Channel] Received data from certificate api.'
                            '  CompletedDate: {completed_date}, Course: {course_id}, Enterprise: {enterprise},'
                            ' Grade: {grade}, IsPassing: {is_passing}, User: {user_id}'.format(
                                completed_date=completed_date_from_api,
                                grade=grade_from_api,
                                is_passing=is_passing_from_api,
                                course_id=course_id,
                                user_id=enterprise_enrollment.enterprise_customer_user.user_id,
                                enterprise=enterprise_enrollment.enterprise_customer_user.enterprise_customer.slug))
            # For self-paced courses, check the Grades API
            else:
                completed_date_from_api, grade_from_api, is_passing_from_api = \
                    self._collect_grades_data(enterprise_enrollment, course_details)
                LOGGER.info('[Integrated Channel] Received data from grades api.'
                            '  CompletedDate: {completed_date}, Course: {course_id}, Enterprise: {enterprise},'
                            ' Grade: {grade}, IsPassing: {is_passing}, User: {user_id}'.format(
                                completed_date=completed_date_from_api,
                                grade=grade_from_api,
                                is_passing=is_passing_from_api,
                                course_id=course_id,
                                user_id=enterprise_enrollment.enterprise_customer_user.user_id,
                                enterprise=enterprise_enrollment.enterprise_customer_user.enterprise_customer.slug))
            if exporting_single_learner and (grade != grade_from_api or is_passing != is_passing_from_api or
                                             completed_date != completed_date_from_api):
                enterprise_user = enterprise_enrollment.enterprise_customer_user
                LOGGER.error('[Integrated Channel] Attempt to transmit conflicting data. '
                             ' CompletedDate: {completed_date}, CompletedDateAPI: {completed_date_api},'
                             ' Course: {course_id}, Enterprise: {enterprise},'
                             ' EnrollmentId: {enrollment_id},'
                             ' Grade: {grade}, GradeAPI: {grade_api}, IsPassing: {is_passing},'
                             ' IsPassingAPI: {is_passing_api}, User: {user_id}'.format(
                                 completed_date=completed_date,
                                 grade=grade,
                                 is_passing=is_passing,
                                 completed_date_api=completed_date_from_api,
                                 grade_api=grade_from_api,
                                 is_passing_api=is_passing_from_api,
                                 course_id=course_id,
                                 enrollment_id=enterprise_enrollment.id,
                                 user_id=enterprise_user.user_id,
                                 enterprise=enterprise_user.enterprise_customer.slug))
            # Apply the Single Source of Truth for Grades
            grade = grade_from_api
            completed_date = completed_date_from_api
            is_passing = is_passing_from_api
            records = self.get_learner_data_records(
                enterprise_enrollment=enterprise_enrollment,
                completed_date=completed_date,
                grade=grade,
                is_passing=is_passing,
            )

            if records:
                # There are some cases where we won't receive a record from the above
                # method; right now, that should only happen if we have an Enterprise-linked
                # user for the integrated channel, and transmission of that user's
                # data requires an upstream user identifier that we don't have (due to a
                # failure of SSO or similar). In such a case, `get_learner_data_record`
                # would return None, and we'd simply skip yielding it here.
                for record in records:
                    yield record

    def get_learner_data_records(self, enterprise_enrollment, completed_date=None, grade=None, is_passing=False):
        """
        Generate a learner data transmission audit with fields properly filled in.
        """
        # pylint: disable=invalid-name
        LearnerDataTransmissionAudit = apps.get_model('integrated_channel', 'LearnerDataTransmissionAudit')
        completed_timestamp = None
        course_completed = False
        if completed_date is not None:
            completed_timestamp = parse_datetime_to_epoch_millis(completed_date)
            course_completed = is_passing

        return [
            LearnerDataTransmissionAudit(
                enterprise_course_enrollment_id=enterprise_enrollment.id,
                course_id=enterprise_enrollment.course_id,
                course_completed=course_completed,
                completed_timestamp=completed_timestamp,
                grade=grade,
            )
        ]

    def _collect_certificate_data(self, enterprise_enrollment):
        """
        Collect the learner completion data from the course certificate.

        Used for Instructor-paced courses.

        If no certificate is found, then returns the completed_date = None, grade = In Progress, on the idea that a
        certificate will eventually be generated.

        Args:
            enterprise_enrollment (EnterpriseCourseEnrollment): the enterprise enrollment record for which we need to
            collect completion/grade data

        Returns:
            completed_date: Date the course was completed, this is None if course has not been completed.
            grade: Current grade in the course.
            is_passing: Boolean indicating if the grade is a passing grade or not.
        """

        if self.certificates_api is None:
            self.certificates_api = CertificatesApiClient(self.user)

        course_id = enterprise_enrollment.course_id
        username = enterprise_enrollment.enterprise_customer_user.user.username

        try:
            certificate = self.certificates_api.get_course_certificate(course_id, username)
            completed_date = certificate.get('created_date')
            if completed_date:
                completed_date = parse_datetime(completed_date)
            else:
                completed_date = timezone.now()

            # For consistency with _collect_grades_data, we only care about Pass/Fail grades. This could change.
            is_passing = certificate.get('is_passing')
            grade = self.grade_passing if is_passing else self.grade_failing

        except HttpNotFoundError:
            LOGGER.error('[Integrated Channel] Certificate data not found.'
                         ' Course: {course_id}, EnterpriseEnrollment: {enterprise_enrollment},'
                         ' Username: {username}'.format(
                             course_id=course_id,
                             username=username,
                             enterprise_enrollment=enterprise_enrollment.pk))
            completed_date = None
            grade = self.grade_incomplete
            is_passing = False

        return completed_date, grade, is_passing

    def _collect_grades_data(self, enterprise_enrollment, course_details):
        """
        Collect the learner completion data from the Grades API.

        Used for self-paced courses.

        Args:
            enterprise_enrollment (EnterpriseCourseEnrollment): the enterprise enrollment record for which we need to
            collect completion/grade data
            course_details (dict): the course details for the course in the enterprise enrollment record.

        Returns:
            completed_date: Date the course was completed, this is None if course has not been completed.
            grade: Current grade in the course.
            is_passing: Boolean indicating if the grade is a passing grade or not.
        """
        if self.grades_api is None:
            self.grades_api = GradesApiClient(self.user)

        course_id = enterprise_enrollment.course_id
        username = enterprise_enrollment.enterprise_customer_user.user.username

        try:
            grades_data = self.grades_api.get_course_grade(course_id, username)

        except HttpNotFoundError as error:
            # Grade not found, so we have nothing to report.
            if hasattr(error, 'content'):
                response_content = json.loads(error.content)
                if response_content.get('error_code', '') == 'user_not_enrolled':
                    # This means the user has an enterprise enrollment record but is not enrolled in the course yet
                    LOGGER.info(
                        '[Integrated Channel] User is not enrolled in the course.'
                        ' Course: {course_id}, EnterpriseEnrollment: {enterprise_enrollment},'
                        ' Username: {username}'.format(
                            course_id=course_id,
                            username=username,
                            enterprise_enrollment=enterprise_enrollment.pk))
                    return None, None, None

            LOGGER.error('[Integrated Channel] Grades data not found.'
                         ' Course: {course_id}, EnterpriseEnrollment: {enterprise_enrollment},'
                         ' Username: {username}'.format(
                             course_id=course_id,
                             username=username,
                             enterprise_enrollment=enterprise_enrollment.pk))
            return None, None, None

        # Prepare to process the course end date and pass/fail grade
        course_end_date = course_details.get('end')
        if course_end_date is not None:
            course_end_date = parse_datetime(course_end_date)
        now = timezone.now()
        is_passing = grades_data.get('passed')

        # We can consider a course complete if:
        # * the course's end date has passed
        if course_end_date is not None and course_end_date < now:
            completed_date = course_end_date
            grade = self.grade_passing if is_passing else self.grade_failing

        # * Or, the learner has a passing grade (as of now)
        elif is_passing:
            completed_date = now
            grade = self.grade_passing

        # Otherwise, the course is still in progress
        else:
            completed_date = None
            grade = self.grade_incomplete

        return completed_date, grade, is_passing

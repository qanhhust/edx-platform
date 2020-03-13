"""
Management command to bulk update many user's email addresses
"""


import csv
import logging
import unicodecsv

from os import path

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.http import int_to_base36
from edx_ace import ace
from edx_ace.recipient import Recipient

from openedx.core.djangoapps.ace_common.template_context import get_base_template_context
from openedx.core.djangoapps.lang_pref import LANGUAGE_KEY
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.user_api.preferences.api import get_user_preference
from openedx.core.djangoapps.user_authn.message_types import PasswordReset
from openedx.core.lib.celery.task_utils import emulate_http_request

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Command(BaseCommand):
    """
        Management command to recover account for the learners got their accounts taken
        over due to bad passwords. Learner's email address will be updated and password
        reset email would be sent to these learner's.
    """

    help = """
        Change the email address of each user specified in the csv file and
        send password reset email.

        csv file is expected to have one row per user with the format:
        username, email_address, new_email_address

        Example:
            $ ... recover_account csv_file_path
        """

    def add_arguments(self, parser):
        """ Add argument to the command parser. """
        parser.add_argument(
            '--csv_file_path',
            required=True,
            help='Csv file path'
        )

    def handle(self, *args, **options):
        """ Main handler for the command."""
        file_path = options['csv_file_path']

        if not path.isfile(file_path):
            raise CommandError('File not found.')

        with open(file_path, 'rb') as csv_file:
            csv_reader = list(unicodecsv.DictReader(csv_file))

        successful_updates = []
        failed_updates = []
        site = Site.objects.get_current()

        for row in csv_reader:
            username = row['username']
            email = row['email']
            new_email = row['new_email']
            try:
                user = get_user_model().objects.get(Q(username=username) | Q(email__iexact=email))
                user.email = new_email
                user.save()
                self.send_password_reset_email(user, email, site)
                successful_updates.append(new_email)
            except (ObjectDoesNotExist, Exception) as exc:  # pylint: disable=broad-except
                logger.exception('Unable to send email to {email} and exception was {exp}'.format(
                    email=email, exp=exc
                ))
                failed_updates.append(email)

        logger.info(
             'Successfully updated {successful} accounts. Failed to update {failed} accounts'.format(
                 successful=successful_updates, failed=failed_updates
             )
        )

    def send_password_reset_email(self, user, email, site):
        message_context = get_base_template_context(site)
        message_context.update({
            'email': email,
            'platform_name': configuration_helpers.get_value('PLATFORM_NAME', settings.PLATFORM_NAME),
            'reset_link': '{protocol}://{site}{link}?track=pwreset'.format(
                protocol='http',
                site=configuration_helpers.get_value('SITE_NAME', settings.SITE_NAME),
                link=reverse('password_reset_confirm', kwargs={
                    'uidb36': int_to_base36(user.id),
                    'token': default_token_generator.make_token(user),
                }),
            )
        })

        with emulate_http_request(site, user):
            msg = PasswordReset().personalize(
                recipient=Recipient(user.username, email),
                language=get_user_preference(user, LANGUAGE_KEY),
                user_context=message_context,
            )
            ace.send(msg)

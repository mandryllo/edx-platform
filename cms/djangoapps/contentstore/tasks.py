"""
This file contains celery tasks for contentstore views
"""
import json
import logging
from celery.task import task
from celery.utils.log import get_task_logger
from datetime import datetime
from pytz import UTC

from django.contrib.auth.models import User

from contentstore.courseware_index import CoursewareSearchIndexer, LibrarySearchIndexer, SearchIndexingError
from contentstore.utils import initialize_permissions
from course_action_state.models import CourseRerunState
from opaque_keys.edx.keys import CourseKey
from xmodule.course_module import CourseFields
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.exceptions import DuplicateCourseError, ItemNotFoundError

from student.models import UserProfile
from contentstore.utils import delete_course_and_groups
from search.search_engine_base import SearchEngine
from contentstore.courseware_index import CourseAboutSearchIndexer
from contentstore.helpers import (get_courses_accessible_to_user,
                                  accessible_libraries_list,
                                  format_course_for_view,
                                  format_library_for_view)

LOGGER = get_task_logger(__name__)
FULL_COURSE_REINDEX_THRESHOLD = 1


@task()
def rerun_course(source_course_key_string, destination_course_key_string, user_id, fields=None):
    """
    Reruns a course in a new celery task.
    """
    # import here, at top level this import prevents the celery workers from starting up correctly
    from edxval.api import copy_course_videos

    try:
        # deserialize the payload
        destination_course_key = CourseKey.from_string(destination_course_key_string)
        fields = deserialize_fields(fields) if fields else None

        # use the split modulestore as the store for the rerun course,
        # as the Mongo modulestore doesn't support multiple runs of the same course.
        store = modulestore()
        with store.default_store('split'):
            store.clone_course(source_course_key, destination_course_key, user_id, fields=fields)

        # set initial permissions for the user to access the course.
        initialize_permissions(destination_course_key, User.objects.get(id=user_id))

        # update state: Succeeded
        CourseRerunState.objects.succeeded(course_key=destination_course_key)

        # call edxval to attach videos to the rerun
        copy_course_videos(source_course_key, destination_course_key)

        return "succeeded"

    except DuplicateCourseError as exc:
        # do NOT delete the original course, only update the status
        CourseRerunState.objects.failed(course_key=destination_course_key)
        logging.exception(u'Course Rerun Error')
        return "duplicate course"

    # catch all exceptions so we can update the state and properly cleanup the course.
    except Exception as exc:  # pylint: disable=broad-except
        # update state: Failed
        CourseRerunState.objects.failed(course_key=destination_course_key)
        logging.exception(u'Course Rerun Error')

        try:
            # cleanup any remnants of the course
            modulestore().delete_course(destination_course_key, user_id)
        except ItemNotFoundError:
            # it's possible there was an error even before the course module was created
            pass

        return "exception: " + unicode(exc)


def deserialize_fields(json_fields):
    fields = json.loads(json_fields)
    for field_name, value in fields.iteritems():
        fields[field_name] = getattr(CourseFields, field_name).from_json(value)
    return fields


def _parse_time(time_isoformat):
    """ Parses time from iso format """
    return datetime.strptime(
        # remove the +00:00 from the end of the formats generated within the system
        time_isoformat.split('+')[0],
        "%Y-%m-%dT%H:%M:%S.%f"
    ).replace(tzinfo=UTC)


@task()
def update_search_index(course_id, triggered_time_isoformat):
    """ Updates course search index. """
    try:
        course_key = CourseKey.from_string(course_id)
        CoursewareSearchIndexer.index(modulestore(), course_key, triggered_at=(_parse_time(triggered_time_isoformat)))

    except SearchIndexingError as exc:
        LOGGER.error('Search indexing error for complete course %s - %s', course_id, unicode(exc))
    else:
        LOGGER.debug('Search indexing successful for complete course %s', course_id)


@task()
def update_library_index(library_id, triggered_time_isoformat):
    """ Updates course search index. """
    try:
        library_key = CourseKey.from_string(library_id)
        LibrarySearchIndexer.index(modulestore(), library_key, triggered_at=(_parse_time(triggered_time_isoformat)))

    except SearchIndexingError as exc:
        LOGGER.error('Search indexing error for library %s - %s', library_id, unicode(exc))
    else:
        LOGGER.debug('Search indexing successful for library %s', library_id)


@task()
def push_course_update_task(course_key_string, course_subscription_id, course_display_name):
    """
    Sends a push notification for a course update.
    """
    # TODO Use edx-notifications library instead (MA-638).
    from .push_notification import send_push_course_update
    send_push_course_update(course_key_string, course_subscription_id, course_display_name)


@task()
def delete_course_task(user_id, course_key_string):

    profile = UserProfile.objects.get(pk=user_id)
    user = User.objects.get(pk=profile.user_id)

    course_key = CourseKey.from_string(course_key_string)
    delete_course_and_groups(course_key, user.id)
    searcher = SearchEngine.get_search_engine(CoursewareSearchIndexer.INDEX_NAME)
    if searcher != None:
        CoursewareSearchIndexer.remove_deleted_items(searcher, CourseKey.from_string(course_key_string), [])
        searcher.remove(CourseAboutSearchIndexer.DISCOVERY_DOCUMENT_TYPE, [course_key_string])


@task()
def delete_library_task(user_id, library_key_string):

    profile = UserProfile.objects.get(pk=user_id)
    user = User.objects.get(pk=profile.user_id)

    library_key = CourseKey.from_string(library_key_string)
    delete_course_and_groups(library_key, user.id)


@task()
def delete_temp_user_task(request, user_id):

    profile = UserProfile.objects.get(pk=user_id)
    user = User.objects.get(pk=profile.user_id)

    courses = [format_course_for_view(c) for c in get_courses_accessible_to_user(request, user)[0]]
    libraries = [format_library_for_view(lib, user) for lib in accessible_libraries_list(user)]

    for course in courses:
        course_key = CourseKey.from_string(course["course_key"])
        delete_course_and_groups(course_key, user.id)
        searcher = SearchEngine.get_search_engine(CoursewareSearchIndexer.INDEX_NAME)
        if searcher != None:
            CoursewareSearchIndexer.remove_deleted_items(searcher, CourseKey.from_string(course_key_string), [])
            searcher.remove(CourseAboutSearchIndexer.DISCOVERY_DOCUMENT_TYPE, [course_key_string])

    for library in libraries:
        library_key = CourseKey.from_string(library['library_key'])
        delete_course_and_groups(library_key, user.id)
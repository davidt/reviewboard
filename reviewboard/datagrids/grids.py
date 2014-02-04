from __future__ import unicode_literals

import pytz

from django.contrib.auth.models import User
from django.http import Http404
from django.utils.datastructures import SortedDict
from django.utils.translation import ugettext_lazy as _
from djblets.datagrid.grids import (CheckboxColumn, Column, DateTimeColumn,
                                    DataGrid)
from djblets.util.templatetags.djblets_utils import ageid

from reviewboard.accounts.models import Profile, LocalSiteProfile
from reviewboard.datagrids.columns import (BugsColumn,
                                           DateTimeSinceColumn,
                                           DiffUpdatedColumn,
                                           DiffUpdatedSinceColumn,
                                           GroupMemberCountColumn,
                                           GroupsColumn,
                                           MyCommentsColumn,
                                           NewUpdatesColumn,
                                           PendingCountColumn,
                                           PeopleColumn,
                                           RepositoryColumn,
                                           ReviewCountColumn,
                                           ReviewGroupStarColumn,
                                           ReviewRequestIDColumn,
                                           ReviewRequestStarColumn,
                                           ShipItColumn,
                                           SubmitterColumn,
                                           SummaryColumn,
                                           ToMeColumn)
from reviewboard.reviews.models import Group, ReviewRequest
from reviewboard.site.urlresolvers import local_site_reverse


class ReviewRequestDataGrid(DataGrid):
    """A datagrid that displays a list of review requests.

    This datagrid accepts the show_closed parameter in the URL, allowing
    submitted review requests to be filtered out or displayed.
    """
    my_comments = MyCommentsColumn()
    star = ReviewRequestStarColumn()
    ship_it = ShipItColumn()
    summary = SummaryColumn(expand=True, link=True, css_class='summary')
    submitter = SubmitterColumn()

    branch = Column(_('Branch'), db_field='branch',
                    shrink=True, sortable=True, link=False)
    bugs_closed = BugsColumn()
    repository = RepositoryColumn()
    time_added = DateTimeColumn(
        _('Posted'),
        detailed_label=_('Posted Time'),
        format='F jS, Y, P', shrink=True,
        css_class=lambda r: ageid(r.time_added))
    last_updated = DateTimeColumn(
        _('Last Updated'),
        format='F jS, Y, P', shrink=True,
        db_field='last_updated',
        field_name='last_updated',
        css_class=lambda r: ageid(r.last_updated))
    diff_updated = DiffUpdatedColumn(
        format='F jS, Y, P', shrink=True,
        css_class=lambda r: ageid(r.diffset_history.last_diff_updated))
    time_added_since = DateTimeSinceColumn(
        _('Posted'),
        detailed_label=_('Posted Time (Relative)'),
        field_name='time_added', shrink=True,
        css_class=lambda r: ageid(r.time_added))
    last_updated_since = DateTimeSinceColumn(
        _('Last Updated'),
        detailed_label=_('Last Updated (Relative)'), shrink=True,
        db_field='last_updated',
        field_name='last_updated',
        css_class=lambda r: ageid(r.last_updated))
    diff_updated_since = DiffUpdatedSinceColumn(
        detailed_label=_('Diff Updated (Relative)'),
        shrink=True,
        css_class=lambda r: ageid(r.diffset_history.last_diff_updated))

    review_count = ReviewCountColumn()

    target_groups = GroupsColumn()
    target_people = PeopleColumn()
    to_me = ToMeColumn()

    review_id = ReviewRequestIDColumn()

    def __init__(self, *args, **kwargs):
        self.local_site = kwargs.pop('local_site', None)

        super(ReviewRequestDataGrid, self).__init__(*args, **kwargs)

        self.listview_template = 'datagrids/review_request_listview.html'
        self.profile_sort_field = 'sort_review_request_columns'
        self.profile_columns_field = 'review_request_columns'
        self.show_closed = True
        self.submitter_url_name = 'user'
        self.default_sort = ['-last_updated']
        self.default_columns = [
            'star', 'summary', 'submitter', 'time_added', 'last_updated_since'
        ]

        # Add local timezone info to the columns
        user = self.request.user
        if user.is_authenticated():
            profile, is_new = Profile.objects.get_or_create(user=user)
            self.timezone = pytz.timezone(profile.timezone)
            self.time_added.timezone = self.timezone
            self.last_updated.timezone = self.timezone
            self.diff_updated.timezone = self.timezone

    def load_extra_state(self, profile):
        if profile:
            self.show_closed = profile.show_closed

        try:
            self.show_closed = (
                int(self.request.GET.get('show-closed',
                                         self.show_closed))
                != 0)
        except ValueError:
            # do nothing
            pass

        if not self.show_closed:
            self.queryset = self.queryset.filter(status='P')

        self.queryset = self.queryset.filter(local_site=self.local_site)

        if profile and self.show_closed != profile.show_closed:
            profile.show_closed = self.show_closed
            return True

        return False

    def post_process_queryset(self, queryset):
        q = queryset.with_counts(self.request.user)
        return super(ReviewRequestDataGrid, self).post_process_queryset(q)

    def link_to_object(self, obj, value):
        if value and isinstance(value, User):
            return local_site_reverse('user', request=self.request,
                                      args=[value])

        return obj.get_absolute_url()


class DashboardDataGrid(ReviewRequestDataGrid):
    """Displays the dashboard.

    The dashboard is the main place where users see what review requests
    are out there that may need their attention.
    """
    new_updates = NewUpdatesColumn()
    my_comments = MyCommentsColumn()
    selected = CheckboxColumn()

    def __init__(self, *args, **kwargs):
        local_site = kwargs.get('local_site', None)

        super(DashboardDataGrid, self).__init__(*args, **kwargs)

        self.listview_template = 'datagrid/listview.html'
        self.profile_sort_field = 'sort_dashboard_columns'
        self.profile_columns_field = 'dashboard_columns'
        self.default_view = 'incoming'
        self.show_closed = False
        self.default_sort = ['-last_updated']
        self.default_columns = [
            'new_updates', 'star', 'summary', 'submitter',
            'time_added', 'last_updated_since'
        ]
        self.counts = {}

        self.local_site = local_site

    def load_extra_state(self, profile):
        group_name = self.request.GET.get('group', '')
        view = self.request.GET.get('view', self.default_view)
        user = self.request.user

        if view == 'outgoing':
            self.queryset = ReviewRequest.objects.from_user(
                user, user, local_site=self.local_site)
            self.title = _('All Outgoing Review Requests')
        elif view == 'mine':
            self.queryset = ReviewRequest.objects.from_user(
                user, user, None, local_site=self.local_site)
            self.title = _('All My Review Requests')
        elif view == 'to-me':
            self.queryset = ReviewRequest.objects.to_user_directly(
                user, user, local_site=self.local_site)
            self.title = _('Incoming Review Requests to Me')
        elif view == 'to-group':
            if group_name:
                # to-group is special because we want to make sure that the
                # group exists and show a 404 if it doesn't. Otherwise, we'll
                # show an empty datagrid with the name.
                try:
                    group = Group.objects.get(name=group_name,
                                              local_site=self.local_site)

                    if not group.is_accessible_by(user):
                        raise Http404
                except Group.DoesNotExist:
                    raise Http404

                self.queryset = ReviewRequest.objects.to_group(
                    group_name, self.local_site, user)
                self.title = _('Incoming Review Requests to %s') % group_name
            else:
                self.queryset = ReviewRequest.objects.to_user_groups(
                    user, user, local_site=self.local_site)
                self.title = _('All Incoming Review Requests to My Groups')
        elif view == 'starred':
            profile, is_new = Profile.objects.get_or_create(user=user)
            self.queryset = profile.starred_review_requests.public(
                user=user, local_site=self.local_site, status=None)
            self.title = _('Starred Review Requests')
        elif view == 'incoming':
            self.queryset = ReviewRequest.objects.to_user(
                user, user, local_site=self.local_site)
            self.title = _('All Incoming Review Requests')
        else:
            raise Http404

        return False


class SubmitterDataGrid(DataGrid):
    """A datagrid showing a list of users registered on Review Board."""
    username = Column(_('Username'), link=True, sortable=True)
    fullname = Column(_('Full Name'), field_name='get_full_name',
                      link=True, expand=True)
    pending_count = PendingCountColumn(_('Pending Reviews'),
                                       field_name='directed_review_requests',
                                       shrink=True)

    def __init__(self, request,
                 queryset=User.objects.filter(is_active=True),
                 title=_('All submitters'),
                 local_site=None):
        if local_site:
            qs = queryset.filter(local_site=local_site)
        else:
            qs = queryset

        super(SubmitterDataGrid, self).__init__(request, qs, title)

        self.default_sort = ['username']
        self.profile_sort_field = 'sort_submitter_columns'
        self.profile_columns_field = 'submitter_columns'
        self.default_columns = [
            'username', 'fullname', 'pending_count'
        ]

    def link_to_object(self, obj, value):
        return local_site_reverse('user', request=self.request,
                                  args=[obj.username])


class GroupDataGrid(DataGrid):
    """A datagrid showing a list of review groups accessible by the user."""
    star = ReviewGroupStarColumn()
    name = Column(_('Group ID'), link=True, sortable=True)
    displayname = Column(_('Group Name'), field_name='display_name',
                         link=True, expand=True)
    pending_count = PendingCountColumn(_('Pending Reviews'),
                                       field_name='review_requests',
                                       link=True,
                                       shrink=True)
    member_count = GroupMemberCountColumn(_('Members'),
                                          field_name='members',
                                          shrink=True)

    def __init__(self, request, title=_('All groups'), *args, **kwargs):
        local_site = kwargs.pop('local_site', None)
        queryset = Group.objects.accessible(request.user,
                                            local_site=local_site)

        super(GroupDataGrid, self).__init__(request, queryset=queryset,
                                            title=title, *args, **kwargs)

        self.profile_sort_field = 'sort_group_columns'
        self.profile_columns_field = 'group_columns'
        self.default_sort = ['name']
        self.default_columns = [
            'star', 'name', 'displayname', 'pending_count'
        ]

    @staticmethod
    def link_to_object(obj, value):
        return obj.get_absolute_url()


class WatchedGroupDataGrid(GroupDataGrid):
    """Shows the list of review groups watched by the user."""
    def __init__(self, request, title=_('Watched groups'), *args, **kwargs):
        local_site = kwargs.pop('local_site', None)

        super(WatchedGroupDataGrid, self).__init__(request, title=title,
                                                   *args, **kwargs)

        user = request.user
        profile, is_new = Profile.objects.get_or_create(user=user)

        self.queryset = profile.starred_groups.all()
        self.queryset = self.queryset.filter(local_site=local_site)

    def link_to_object(self, group, value):
        return '.?view=to-group&group=%s' % group.name


def get_sidebar_counts(user, local_site):
    """Returns counts used for the Dashboard sidebar."""
    profile, is_new = Profile.objects.get_or_create(user=user)

    site_profile, is_new = LocalSiteProfile.objects.get_or_create(
        local_site=local_site,
        user=user,
        profile=profile)

    if is_new:
        site_profile.save()

    counts = {
        'outgoing': site_profile.pending_outgoing_request_count,
        'incoming': site_profile.total_incoming_request_count,
        'to-me': site_profile.direct_incoming_request_count,
        'starred': site_profile.starred_public_request_count,
        'mine': site_profile.total_outgoing_request_count,
        'groups': SortedDict(),
        'starred_groups': SortedDict(),
    }

    for group in Group.objects.filter(
            users=user, local_site=local_site).order_by('name'):
        counts['groups'][group.name] = group.incoming_request_count

    for group in Group.objects.filter(
            starred_by=profile, local_site=local_site).order_by('name'):
        counts['starred_groups'][group.name] = group.incoming_request_count

    return counts

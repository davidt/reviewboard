from __future__ import unicode_literals

import json
import logging
from collections import defaultdict

from django import forms
from django.conf.urls import patterns, url
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseBadRequest
from django.template import RequestContext
from django.template.loader import render_to_string
from django.utils import six
from django.utils.six.moves.urllib.error import HTTPError, URLError
from django.utils.six.moves.urllib.parse import quote
from django.utils.translation import ugettext_lazy as _, ugettext
from django.views.decorators.http import require_POST

from reviewboard.admin.server import build_server_url, get_server_url
from reviewboard.hostingsvcs.errors import (AuthorizationError,
                                            HostingServiceError,
                                            InvalidPlanError,
                                            RepositoryError)
from reviewboard.hostingsvcs.forms import (HostingServiceAuthForm,
                                           HostingServiceForm)
from reviewboard.hostingsvcs.hook_utils import (close_all_review_requests,
                                                get_repository_for_hook,
                                                get_review_request_id)
from reviewboard.hostingsvcs.service import HostingService
from reviewboard.scmtools.core import Branch, Commit
from reviewboard.scmtools.crypto_utils import (decrypt_password,
                                               encrypt_password)
from reviewboard.scmtools.errors import FileNotFoundError
from reviewboard.site.urlresolvers import local_site_reverse


class BitbucketAuthForm(HostingServiceAuthForm):
    class Meta(object):
        help_texts = {
            'hosting_account_username': _(
                'Your Bitbucket username. This must <em>not</em> be your '
                'e-mail address! You can find your username in your '
                '<a href="https://bitbucket.org/account/admin/">Bitbucket '
                'Account Settings</a>.'
            ),
            'hosting_account_password': _(
                'The password used for your account, or a '
                '<a href="https://bitbucket.org/account/admin/app-passwords">'
                'configured app password</a>. <strong>Important:</strong> If '
                'using two-factor authentication, you <em>must</em> use an '
                'app password configured with read access to repositories, '
                'accounts, and projects.'
            ),
        }


class BitbucketPersonalForm(HostingServiceForm):
    bitbucket_repo_name = forms.CharField(
        label=_('Repository name'),
        max_length=64,
        required=True,
        widget=forms.TextInput(attrs={'size': '60'}),
        help_text=_('The username of the user who owns the repository. This '
                    'is the &lt;repo_name&gt; in '
                    'https://bitbucket.org/&lt;username&gt;/'
                    '&lt;repo_name&gt;/'))


class BitbucketOtherUserForm(HostingServiceForm):
    bitbucket_other_user_username = forms.CharField(
        label=_('Username'),
        max_length=64,
        required=True,
        widget=forms.TextInput(attrs={'size': '60'}),
        help_text=_('The username of the user who owns the repository. This '
                    'is the &lt;username&gt; in '
                    'https://bitbucket.org/&lt;username&gt;/'
                    '&lt;repo_name&gt;/'))

    bitbucket_other_user_repo_name = forms.CharField(
        label=_('Repository name'),
        max_length=64,
        required=True,
        widget=forms.TextInput(attrs={'size': '60'}),
        help_text=_('The name of the repository. This is the '
                    '&lt;repo_name&gt; in '
                    'https://bitbucket.org/&lt;username&gt;/'
                    '&lt;repo_name&gt;/'))


class BitbucketTeamForm(HostingServiceForm):
    bitbucket_team_name = forms.CharField(
        label=_('Team name'),
        max_length=64,
        required=True,
        widget=forms.TextInput(attrs={'size': '60'}),
        help_text=_('The name of the team. This is the &lt;team_name&gt; in '
                    'https://bitbucket.org/&lt;team_name&gt;/'
                    '&lt;repo_name&gt;/'))

    bitbucket_team_repo_name = forms.CharField(
        label=_('Repository name'),
        max_length=64,
        required=True,
        widget=forms.TextInput(attrs={'size': '60'}),
        help_text=_('The name of the repository. This is the '
                    '&lt;repo_name&gt; in '
                    'https://bitbucket.org/&lt;team_name&gt;/'
                    '&lt;repo_name&gt;/'))


class Bitbucket(HostingService):
    """Hosting service support for Bitbucket.

    Bitbucket is a hosting service that supports Git and Mercurial
    repositories, and provides issue tracker support. It's available
    at https://www.bitbucket.org/.
    """

    name = 'Bitbucket'
    auth_form = BitbucketAuthForm

    needs_authorization = True
    supports_repositories = True
    supports_bug_trackers = True
    supports_post_commit = True

    has_repository_hook_instructions = True

    repository_url_patterns = patterns(
        '',

        url(r'^hooks/(?P<hooks_uuid>[a-z0-9]+)/close-submitted/$',
            'reviewboard.hostingsvcs.bitbucket'
            '.post_receive_hook_close_submitted',
            name='bitbucket-hooks-close-submitted'),
    )

    supported_scmtools = ['Git', 'Mercurial']
    plans = [
        ('personal', {
            'name': _('Personal'),
            'form': BitbucketPersonalForm,
            'repository_fields': {
                'Git': {
                    'path': 'git@bitbucket.org:%(hosting_account_username)s/'
                            '%(bitbucket_repo_name)s.git',
                    'mirror_path': 'https://%(hosting_account_username)s@'
                                   'bitbucket.org/'
                                   '%(hosting_account_username)s/'
                                   '%(bitbucket_repo_name)s.git',
                },
                'Mercurial': {
                    'path': 'https://%(hosting_account_username)s@'
                            'bitbucket.org/%(hosting_account_username)s/'
                            '%(bitbucket_repo_name)s',
                    'mirror_path': 'ssh://hg@bitbucket.org/'
                                   '%(hosting_account_username)s/'
                                   '%(bitbucket_repo_name)s',
                },
            },
            'bug_tracker_field': ('https://bitbucket.org/'
                                  '%(hosting_account_username)s/'
                                  '%(bitbucket_repo_name)s/issue/%%s/'),
        }),
        ('other-user', {
            'name': _('Other User'),
            'form': BitbucketOtherUserForm,
            'repository_fields': {
                'Git': {
                    'path': 'git@bitbucket.org:'
                            '%(bitbucket_other_user_username)s/'
                            '%(bitbucket_other_user_repo_name)s.git',
                    'mirror_path': 'https://%(hosting_account_username)s@'
                                   'bitbucket.org/'
                                   '%(bitbucket_other_user_username)s/'
                                   '%(bitbucket_other_user_repo_name)s.git',
                },
                'Mercurial': {
                    'path': 'https://%(hosting_account_username)s@'
                            'bitbucket.org/%(bitbucket_other_user_username)s/'
                            '%(bitbucket_other_user_repo_name)s',
                    'mirror_path': 'ssh://hg@bitbucket.org/'
                                   '%(bitbucket_other_user_username)s/'
                                   '%(bitbucket_other_user_repo_name)s',
                },
            },
            'bug_tracker_field': ('https://bitbucket.org/'
                                  '%(bitbucket_other_user_username)s/'
                                  '%(bitbucket_other_user_repo_name)s/'
                                  'issue/%%s/'),
        }),
        ('team', {
            'name': _('Team'),
            'form': BitbucketTeamForm,
            'repository_fields': {
                'Git': {
                    'path': 'git@bitbucket.org:%(bitbucket_team_name)s/'
                            '%(bitbucket_team_repo_name)s.git',
                    'mirror_path': 'https://%(hosting_account_username)s@'
                                   'bitbucket.org/%(bitbucket_team_name)s/'
                                   '%(bitbucket_team_repo_name)s.git',
                },
                'Mercurial': {
                    'path': 'https://%(hosting_account_username)s@'
                            'bitbucket.org/%(bitbucket_team_name)s/'
                            '%(bitbucket_team_repo_name)s',
                    'mirror_path': 'ssh://hg@bitbucket.org/'
                                   '%(bitbucket_team_name)s/'
                                   '%(bitbucket_team_repo_name)s',
                },
            },
            'bug_tracker_field': ('https://bitbucket.org/'
                                  '%(bitbucket_team_name)s/'
                                  '%(bitbucket_team_repo_name)s/issue/%%s/'),

        }),
    ]

    DEFAULT_PLAN = 'personal'

    def check_repository(self, plan=DEFAULT_PLAN, *args, **kwargs):
        """Checks the validity of a repository.

        This will perform an API request against Bitbucket to get
        information on the repository. This will throw an exception if
        the repository was not found, and return cleanly if it was found.
        """
        repo_name = self._get_repository_name_raw(plan, kwargs)

        if '/' in repo_name:
            raise RepositoryError(ugettext(
                'Please specify just the name of the repository, not '
                'a path.'))

        if '.git' in repo_name:
            raise RepositoryError(ugettext(
                'Please specify just the name of the repository without '
                '".git".'))

        try:
            self._api_get_repository(
                self._get_repository_owner_raw(plan, kwargs),
                self._get_repository_name_raw(plan, kwargs))
        except HostingServiceError as e:
            if six.text_type(e) == 'Resource not found':
                raise RepositoryError(
                    ugettext('A repository with this name was not found.'))

            raise

    def authorize(self, username, password, *args, **kwargs):
        """Authorizes the Bitbucket repository.

        Bitbucket supports HTTP Basic Auth or OAuth for the API. We use
        HTTP Basic Auth for now, and we store provided password,
        encrypted, for use in later API requests.
        """
        self.account.data['password'] = encrypt_password(password)

        try:
            self._api_get(self._build_api_url('user'))
            self.account.save()
        except HostingServiceError as e:
            del self.account.data['password']

            if e.http_code in (401, 403):
                self._raise_auth_error()
            else:
                raise
        except Exception:
            del self.account.data['password']
            raise

    def is_authorized(self):
        """Determines if the account has supported authorization tokens.

        This just checks if there's a password set on the account.
        """
        return self.account.data.get('password', None) is not None

    def get_file(self, repository, path, revision, base_commit_id=None,
                 *args, **kwargs):
        """Fetches a file from Bitbucket.

        This will perform an API request to fetch the contents of a file.

        If using Git, this will expect a base commit ID to be provided.
        """
        try:
            return self._api_get_src(repository, path, revision,
                                     base_commit_id)
        except (URLError, HTTPError):
            raise FileNotFoundError(path, revision)

    def get_file_exists(self, repository, path, revision, base_commit_id=None,
                        *args, **kwargs):
        """Determines if a file exists.

        This will perform an API request to fetch the metadata for a file.

        If using Git, this will expect a base commit ID to be provided.
        """
        try:
            self._api_get_src(repository, path, revision, base_commit_id)

            return True
        except (URLError, HTTPError, FileNotFoundError):
            return False

    def get_repository_hook_instructions(self, request, repository):
        """Returns instructions for setting up incoming webhooks."""
        webhook_endpoint_url = build_server_url(local_site_reverse(
            'bitbucket-hooks-close-submitted',
            local_site=repository.local_site,
            kwargs={
                'repository_id': repository.pk,
                'hosting_service_id': repository.hosting_account.service_name,
                'hooks_uuid': repository.get_or_create_hooks_uuid(),
            }))
        add_webhook_url = (
            'https://bitbucket.org/%s/%s/admin/hooks?service=POST&url=%s'
            % (self._get_repository_owner(repository),
               self._get_repository_name(repository),
               webhook_endpoint_url))

        example_id = 123
        example_url = build_server_url(local_site_reverse(
            'review-request-detail',
            local_site=repository.local_site,
            kwargs={
                'review_request_id': example_id,
            }))

        return render_to_string(
            'hostingsvcs/bitbucket/repo_hook_instructions.html',
            RequestContext(request, {
                'example_id': example_id,
                'example_url': example_url,
                'repository': repository,
                'server_url': get_server_url(),
                'add_webhook_url': add_webhook_url,
            }))

    def _get_default_branch_name(self, repository):
        """Return the name of the repository's default branch.

        Args:
            repository (reviewboard.scmtools.models.Repository):
                The repository whose default branch is to be looked up.

        Returns:
            unicode: The name of the default branch.
        """
        url = self._build_repository_api_url(repository, 'main-branch/')

        rsp = self._api_get(url)

        return rsp['name']

    def get_branches(self, repository):
        default_branch_name = self._get_default_branch_name(repository)

        url = self._build_repository_api_url(repository, 'branches/')

        rsp = self._api_get(url)

        branches = []

        for branch_name, branch in six.iteritems(rsp):
            branches.append(
                Branch(id=branch_name,
                       commit=branch['raw_node'],
                       default=(branch_name == default_branch_name)))

        return branches

    def get_commits(self, repository, branch=None, start=None):
        url = self._build_repository_api_url(repository,
                                             'changesets/?limit=20')

        start = start or branch

        if start:
            url += '&start=%s' % start

        results = []

        # The API returns them in order from oldest to newest.
        for changeset in reversed(self._api_get(url)['changesets']):
            commit = Commit(
                author_name=changeset['author'],
                id=changeset['raw_node'],
                date=self._parse_timestamp(changeset['utctimestamp']),
                message=changeset['message'])

            if changeset['parents']:
                commit.parent = changeset['parents'][0]

            results.append(commit)

        return results

    def get_change(self, repository, revision):
        # We try to pull the commit's metadata out of the cache. The diff API
        # endpoint is just the raw content of the diff and contains no
        # metadata.
        commit = cache.get(repository.get_commit_cache_key(revision))

        if not commit:
            # However, if it is not in the cache, we have to hit the API to
            # get the metadata.
            commit = self.get_commits(repository, revision)[0]

        url = self._build_repository_api_url(repository, 'diff/%s' % revision,
                                             version='2.0')

        diff = self._api_get(url, raw_content=True)

        if not diff.endswith(b'\n'):
            diff += b'\n'

        return Commit(author_name=commit.author_name,
                      id=commit.id,
                      date=commit.date,
                      message=commit.message,
                      diff=diff,
                      parent=commit.parent)

    def _build_repository_api_url(self, repository, url='', version='1.0'):
        """Build an API URL for the given repository.

        Args:
            repository (reviewboard.scmtools.models.Repository):
                The repository.

            url (unicode):
                Extra url components to add to the end of the generated URL.

            version (unicode):
                The API version to use.

        Returns:
            unicode:
                The API URL.
        """
        username = self._get_repository_owner(repository)
        repo_name = self._get_repository_name(repository)

        return self._build_api_url('repositories/%s/%s/%s'
                                   % (username, repo_name, url),
                                   version=version)

    def _api_get_repository(self, username, repo_name):
        url = self._build_api_url('repositories/%s/%s'
                                  % (username, repo_name))

        return self._api_get(url)

    def _api_get_src(self, repository, path, revision, base_commit_id):
        # If a base commit ID is provided, use it. It may not be provided,
        # though, and in this case, we need to use the provided revision,
        # which will work for Mercurial but not for Git.
        #
        # If not provided, and using Git, we'll give the user a File Not
        # Found error with some info on what they need to do to correct
        # this.
        if base_commit_id:
            revision = base_commit_id
        elif repository.tool.name == 'Git':
            raise FileNotFoundError(
                path,
                revision,
                detail='The necessary revision information needed to find '
                       'this file was not provided. Use RBTools 0.5.2 or '
                       'newer.')

        url = self._build_api_url(
            'repositories/%s/%s/raw/%s/%s'
            % (quote(self._get_repository_owner(repository)),
               quote(self._get_repository_name(repository)),
               quote(revision),
               quote(path)))

        try:
            return self._api_get(url, raw_content=True)
        except FileNotFoundError:
            raise FileNotFoundError(path, revision=revision,
                                    base_commit_id=base_commit_id)

    def _build_api_url(self, url, version='1.0'):
        return 'https://bitbucket.org/api/%s/%s' % (version, url)

    def _get_repository_plan(self, repository):
        return (repository.extra_data.get('repository_plan') or
                self.DEFAULT_PLAN)

    def _get_repository_name(self, repository):
        return self._get_repository_name_raw(
            self._get_repository_plan(repository),
            repository.extra_data)

    def _get_repository_name_raw(self, plan, extra_data):
        if plan == 'personal':
            return extra_data['bitbucket_repo_name']
        elif plan == 'team':
            return extra_data['bitbucket_team_repo_name']
        elif plan == 'other-user':
            return extra_data['bitbucket_other_user_repo_name']
        else:
            raise InvalidPlanError(plan)

    def _get_repository_owner(self, repository):
        return self._get_repository_owner_raw(
            self._get_repository_plan(repository),
            repository.extra_data)

    def _get_repository_owner_raw(self, plan, extra_data):
        if plan == 'personal':
            return self.account.username
        elif plan == 'team':
            return extra_data['bitbucket_team_name']
        elif plan == 'other-user':
            return extra_data['bitbucket_other_user_username']
        else:
            raise InvalidPlanError(plan)

    def _api_get(self, url, raw_content=False):
        try:
            data, headers = self.client.http_get(
                url,
                username=self.account.username,
                password=decrypt_password(self.account.data['password']))

            if raw_content:
                return data
            else:
                return json.loads(data)
        except HTTPError as e:
            self._check_api_error(e)

    def _check_api_error(self, e):
        data = e.read()

        try:
            rsp = json.loads(data)
        except:
            rsp = None

        message = data

        if rsp and 'error' in rsp:
            error = rsp['error']

            if 'message' in error:
                message = error['message']

        if message:
            message = six.text_type(message)

        if e.code == 401:
            self._raise_auth_error(message)
        elif e.code == 404:
            if message.startswith('Repository'):
                raise HostingServiceError(message, http_code=e.code)

            # We don't have a path here, but it will be filled in inside
            # _api_get_src.
            raise FileNotFoundError('')
        else:
            raise HostingServiceError(
                message or (
                    ugettext('Unexpected HTTP %s error when talking to '
                             'Bitbucket')
                    % e.code),
                http_code=e.code)

    def _parse_timestamp(self, timestamp):
        """Parse a timestamp given by BitBucket's API into the correct format.

        BitBucket gives timestamps in the form ``YYYY-MM-DD HH:MM:SS+ZZZZ``,
        but JavaScript's ``Date`` cannot parse them in this format; it expects
        the format ``YYYY-MM-DDTHH:MM:SS+ZZZZ`` (where T is a literal T).

        Args:
            timestamp (unicode):
                A string representing a UTC timestamp.

        Returns:
            unicode: A string representing a UTC timestamp in ISO 8601 format.
        """
        return timestamp.replace(' ', 'T')

    def _raise_auth_error(self, message=None):
        raise AuthorizationError(
            message or ugettext(
                'Invalid Bitbucket username or password. Make sure '
                'you are using your Bitbucket username and not e-mail '
                'address, and are using an app password if two-factor '
                'authentication is enabled.'))


@require_POST
def post_receive_hook_close_submitted(request, local_site_name=None,
                                      repository_id=None,
                                      hosting_service_id=None,
                                      hooks_uuid=None):
    """Closes review requests as submitted automatically after a push."""
    repository = get_repository_for_hook(repository_id, hosting_service_id,
                                         local_site_name, hooks_uuid)

    if 'payload' not in request.POST:
        return HttpResponseBadRequest('Missing payload')

    try:
        payload = json.loads(request.POST['payload'])
    except ValueError as e:
        logging.error('The payload is not in JSON format: %s', e)
        return HttpResponseBadRequest('Invalid payload format')

    server_url = get_server_url(request=request)
    review_request_id_to_commits = \
        _get_review_request_id_to_commits_map(payload, server_url, repository)

    if review_request_id_to_commits:
        close_all_review_requests(review_request_id_to_commits,
                                  local_site_name, repository,
                                  hosting_service_id)

    return HttpResponse()


def _get_review_request_id_to_commits_map(payload, server_url, repository):
    """Returns a dictionary, mapping a review request ID to a list of commits.

    If a commit's commit message does not contain a review request ID, we
    append the commit to the key None.
    """
    review_request_id_to_commits_map = defaultdict(list)
    commits = payload.get('commits', [])

    for commit in commits:
        commit_hash = commit.get('raw_node')
        commit_message = commit.get('message')
        branch_name = commit.get('branch')

        if branch_name:
            review_request_id = get_review_request_id(
                commit_message, server_url, commit_hash, repository)
            review_request_id_to_commits_map[review_request_id].append(
                '%s (%s)' % (branch_name, commit_hash[:7]))

    return review_request_id_to_commits_map

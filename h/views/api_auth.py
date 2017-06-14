# -*- coding: utf-8 -*-

from __future__ import unicode_literals

from urlparse import urlparse, parse_qs, urlunparse
from urllib import urlencode

from pyramid.httpexceptions import HTTPBadRequest, HTTPFound
from pyramid.view import view_config, view_defaults

from h.exceptions import OAuthTokenError
from h.util.view import cors_json_view
from h.util.datetime import utc_iso8601

# Hardcoded list of origins for "trusted" clients.
# In the actual implementation, this will be registered in the DB.
TRUSTED_ORIGINS = ['http://localhost:4000',
                   'http://localhost:5000',
                   'chrome-extension://lhieaifenniokmcmfdcgabhbnjmelchg']

TRUSTED_CLIENT_IDS = ['c622f70e-452e-11e7-8035-a7db3310f162']


@view_defaults(route_name='oauth_authorize')
class OAuthAuthorizeController(object):

    def __init__(self, request):
        self.request = request
        self.oauth_svc = self.request.find_service(name='oauth')
        self.user_svc = self.request.find_service(name='user')

    @view_config(request_method='GET',
                 renderer='h:templates/oauth/authorize.html.jinja2')
    def get(self):
        """
        Check the user's authentication status and present the authorization
        page.
        """
        self._check_params()

        if self.request.authenticated_userid is None:
            return HTTPFound(self.request.route_url('login', _query={
                              'next': self.request.url}))

        params = self.request.params
        user = self.user_svc.fetch(self.request.authenticated_userid)

        return {'username': user.username,
                'client_name': 'Hypothesis',
                'client_id': params['client_id'],
                'response_type': params['response_type'],
                'response_mode': params['response_mode'],
                'origin': params.get('origin'),
                'redirect_uri': params.get('redirect_uri'),
                'state': params.get('state')}

    @view_config(request_method='POST',
                 renderer='h:templates/oauth/post_authorize.html.jinja2')
    def post(self):
        """
        Process an authentication request and return a grant token to the
        client.

        Depending on the "response_mode" parameter the grant token will be
        delivered either via query or fragment parameters in a redirect or via
        a `postMessage` call to the opening window.
        """
        authclient = self._check_params()

        user = self.user_svc.fetch(self.request.authenticated_userid)
        grant_token = self.oauth_svc.create_grant_token(user, authclient)

        params = self.request.params
        if params['response_mode'] == 'web_message':
            return {'grant_token': grant_token,
                    'origin': params['origin'],
                    'state': params.get('state')}
        else:
            redirect_uri = params['redirect_uri']

            auth_params = {'code': grant_token}
            if params.get('state'):
                auth_params['state'] = params.get('state')

            if params['response_mode'] == 'query':
                redirect_uri = _update_query(redirect_uri, auth_params)
            else:
                auth_frag = urlencode(auth_params)
                redirect_uri = _update_fragment(redirect_uri, auth_frag)

            raise HTTPFound(location=redirect_uri)

    def _check_params(self):
        """
        Check parameters for the authorization request.

        If the parameters are valid, returns an authclient.
        Otherwise, raises an exception.
        """
        params = self.request.params

        # Validate client ID and response type
        client_id = params.get('client_id', '')
        authclient = self.oauth_svc._get_authclient_by_id(client_id)
        if not authclient:
            raise HTTPBadRequest('Unknown client ID "{}"'.format(client_id))

        # Check that response mode and location matches a pre-registered
        # location for the client.
        response_mode = params.get('response_mode', 'query')
        if response_mode in ['fragment', 'query']:
            redirect_uri = params.get('redirect_uri')
            if redirect_uri is None:
                err = '"redirect_uri" must be specified when response_mode is "query" or "fragment"'
                raise HTTPBadRequest(err)

            # TODO - Get the `redirect_uri` from the AuthClient.
            if redirect_uri != 'http://localhost:4000/index.html':
                err = 'Redirect URI "{}" not valid for client'.format(redirect_uri)
                raise HTTPBadRequest(err)
        elif response_mode == 'web_message':
            origin = params.get('origin')
            if origin is None:
                raise HTTPBadRequest('"origin" must be specified when response_mode is "web_message"')
            if origin not in TRUSTED_ORIGINS:
                err = 'Origin "{}" not valid for client'.format(origin)
                raise HTTPBadRequest(err)
        else:
            raise HTTPBadRequest('Unsupported response mode "{}"'.format(response_mode))

        return authclient


@cors_json_view(request_method='POST', route_name='oauth_token')
def oauth_token(request):
    """
    Process an OAuth token request from a trusted client.

    Trusted clients can retrieve access credentials automatically if the
    user is logged into the web service.

    This endpoint allows CORS requests but only from the pre-registered
    origin that matches the client ID.
    """

    oauth_svc = request.find_service(name='oauth')
    user_svc = request.find_service(name='user')

    # Check that client ID valid and that client is trusted to use implicit
    # authorization.
    client_id = request.POST.get('client_id')
    if client_id is None:
        raise OAuthTokenError('client_id parameter missing',
                              'invalid_clientid',
                              400)

    authclient = oauth_svc._get_authclient_by_id(client_id)
    if not authclient:
        raise OAuthTokenError('Client ID "{}" is not registered'.format(client_id),
                              'invalid_clientid', 400)
    if client_id not in TRUSTED_CLIENT_IDS:
        raise OAuthTokenError('Client is not trusted for implicit authorization',
                              'untrusted_client', 400)

    # Verify request came from client by checking "Origin" header against
    # pre-registered origin associated with client.
    origin = request.headers.get('origin')
    if origin not in TRUSTED_ORIGINS:
        raise OAuthTokenError(('Request origin "{}" does not match '
                               'registered origin for client').format(origin),
                              'invalid_origin',
                              400)

    # Check that the user is logged in to website, based on the session cookie
    # included with the request.
    if not request.authenticated_userid:
        raise OAuthTokenError('User not authenticated',
                              'unauthenticated_user', 403)

    user = user_svc.fetch(request.authenticated_userid)
    return _access_token_response(oauth_svc, user, authclient)


def _update_query(uri, query):
    """
    Update the query string in a `uri` with values from ` query` dict.
    """
    (scheme, netloc, path, params, q, frag) = urlparse(uri)
    q_dict = parse_qs(q)
    q_dict.update(query)
    new_qs = urlencode(q_dict)
    return urlunparse((scheme, netloc, path, params, new_qs, frag))


def _update_fragment(uri, fragment):
    """
    Replace the `fragment` part of a `uri`.
    """
    (scheme, netloc, path, params, query, _) = urlparse(uri)
    return urlunparse((scheme, netloc, path, params, query, fragment))


@cors_json_view(route_name='token', request_method='POST')
def access_token(request):
    svc = request.find_service(name='oauth')
    user, authclient = svc.verify_token_request(request.POST)
    return _access_token_response(svc, user, authclient)


@cors_json_view(route_name='api.debug_token', request_method='GET')
def debug_token(request):
    if not request.auth_token:
        raise OAuthTokenError('Bearer token is missing in Authorization HTTP header',
                              'missing_token',
                              401)

    svc = request.find_service(name='auth_token')
    token = svc.validate(request.auth_token)
    if token is None:
        raise OAuthTokenError('Bearer token does not exist or is expired',
                              'missing_token',
                              401)

    token = svc.fetch(request.auth_token)
    return _present_debug_token(token)


@cors_json_view(context=OAuthTokenError)
def api_token_error(context, request):
    """Handle an expected/deliberately thrown API exception."""
    request.response.status_code = context.status_code
    resp = {'error': context.type}
    if context.message:
        resp['error_description'] = context.message
    return resp


def _present_debug_token(token):
    data = {'userid': token.userid,
            'expires_at': utc_iso8601(token.expires) if token.expires else None,
            'issued_at': utc_iso8601(token.created),
            'expired': token.expired}

    if token.authclient:
        data['client'] = {'id': token.authclient.id,
                          'name': token.authclient.name}

    return data


def _access_token_response(oauth_svc, user, authclient):
    token = oauth_svc.create_token(user, authclient)

    response = {
        'access_token': token.value,
        'token_type': 'bearer',
    }

    if token.expires:
        response['expires_in'] = token.ttl

    if token.refresh_token:
        response['refresh_token'] = token.refresh_token

    return response

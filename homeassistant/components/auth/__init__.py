"""Component to allow users to login and get tokens.

# POST /auth/token

This is an OAuth2 endpoint for granting tokens. We currently support the grant
types "authorization_code" and "refresh_token". Because we follow the OAuth2
spec, data should be send in formatted as x-www-form-urlencoded. Examples will
be in JSON as it's more readable.

## Grant type authorization_code

Exchange the authorization code retrieved from the login flow for tokens.

{
    "grant_type": "authorization_code",
    "code": "411ee2f916e648d691e937ae9344681e"
}

Return value will be the access and refresh tokens. The access token will have
a limited expiration. New access tokens can be requested using the refresh
token.

{
    "access_token": "ABCDEFGH",
    "expires_in": 1800,
    "refresh_token": "IJKLMNOPQRST",
    "token_type": "Bearer"
}

## Grant type refresh_token

Request a new access token using a refresh token.

{
    "grant_type": "refresh_token",
    "refresh_token": "IJKLMNOPQRST"
}

Return value will be a new access token. The access token will have
a limited expiration.

{
    "access_token": "ABCDEFGH",
    "expires_in": 1800,
    "token_type": "Bearer"
}
"""
import logging
import uuid
from datetime import timedelta

import voluptuous as vol

from homeassistant.auth.models import User, Credentials
from homeassistant.components import websocket_api
from homeassistant.components.http.ban import log_invalid_auth
from homeassistant.components.http.data_validator import RequestDataValidator
from homeassistant.components.http.view import HomeAssistantView
from homeassistant.core import callback
from homeassistant.util import dt as dt_util
from . import indieauth
from . import login_flow

DOMAIN = 'auth'
DEPENDENCIES = ['http']

WS_TYPE_CURRENT_USER = 'auth/current_user'
SCHEMA_WS_CURRENT_USER = websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend({
    vol.Required('type'): WS_TYPE_CURRENT_USER,
})

RESULT_TYPE_CREDENTIALS = 'credentials'
RESULT_TYPE_USER = 'user'

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config):
    """Component to allow users to login."""
    store_result, retrieve_result = _create_auth_code_store()

    hass.http.register_view(GrantTokenView(retrieve_result))
    hass.http.register_view(LinkUserView(retrieve_result))

    hass.components.websocket_api.async_register_command(
        WS_TYPE_CURRENT_USER, websocket_current_user,
        SCHEMA_WS_CURRENT_USER
    )

    await login_flow.async_setup(hass, store_result)

    return True


class GrantTokenView(HomeAssistantView):
    """View to grant tokens."""

    url = '/auth/token'
    name = 'api:auth:token'
    requires_auth = False
    cors_allowed = True

    def __init__(self, retrieve_user):
        """Initialize the grant token view."""
        self._retrieve_user = retrieve_user

    @log_invalid_auth
    async def post(self, request):
        """Grant a token."""
        hass = request.app['hass']
        data = await request.post()

        grant_type = data.get('grant_type')

        if grant_type == 'authorization_code':
            return await self._async_handle_auth_code(hass, data)

        if grant_type == 'refresh_token':
            return await self._async_handle_refresh_token(hass, data)

        return self.json({
            'error': 'unsupported_grant_type',
        }, status_code=400)

    async def _async_handle_auth_code(self, hass, data):
        """Handle authorization code request."""
        client_id = data.get('client_id')
        if client_id is None or not indieauth.verify_client_id(client_id):
            return self.json({
                'error': 'invalid_request',
                'error_description': 'Invalid client id',
            }, status_code=400)

        code = data.get('code')

        if code is None:
            return self.json({
                'error': 'invalid_request',
            }, status_code=400)

        user = self._retrieve_user(client_id, RESULT_TYPE_USER, code)

        if user is None or not isinstance(user, User):
            return self.json({
                'error': 'invalid_request',
                'error_description': 'Invalid code',
            }, status_code=400)

        # refresh user
        user = await hass.auth.async_get_user(user.id)

        if not user.is_active:
            return self.json({
                'error': 'access_denied',
                'error_description': 'User is not active',
            }, status_code=403)

        refresh_token = await hass.auth.async_create_refresh_token(user,
                                                                   client_id)
        access_token = hass.auth.async_create_access_token(refresh_token)

        return self.json({
            'access_token': access_token,
            'token_type': 'Bearer',
            'refresh_token': refresh_token.token,
            'expires_in':
                int(refresh_token.access_token_expiration.total_seconds()),
        })

    async def _async_handle_refresh_token(self, hass, data):
        """Handle authorization code request."""
        client_id = data.get('client_id')
        if client_id is not None and not indieauth.verify_client_id(client_id):
            return self.json({
                'error': 'invalid_request',
                'error_description': 'Invalid client id',
            }, status_code=400)

        token = data.get('refresh_token')

        if token is None:
            return self.json({
                'error': 'invalid_request',
            }, status_code=400)

        refresh_token = await hass.auth.async_get_refresh_token_by_token(token)

        if refresh_token is None:
            return self.json({
                'error': 'invalid_grant',
            }, status_code=400)

        if refresh_token.client_id != client_id:
            return self.json({
                'error': 'invalid_request',
            }, status_code=400)

        access_token = hass.auth.async_create_access_token(refresh_token)

        return self.json({
            'access_token': access_token,
            'token_type': 'Bearer',
            'expires_in':
                int(refresh_token.access_token_expiration.total_seconds()),
        })


class LinkUserView(HomeAssistantView):
    """View to link existing users to new credentials."""

    url = '/auth/link_user'
    name = 'api:auth:link_user'

    def __init__(self, retrieve_credentials):
        """Initialize the link user view."""
        self._retrieve_credentials = retrieve_credentials

    @RequestDataValidator(vol.Schema({
        'code': str,
        'client_id': str,
    }))
    async def post(self, request, data):
        """Link a user."""
        hass = request.app['hass']
        user = request['hass_user']

        credentials = self._retrieve_credentials(
            data['client_id'], RESULT_TYPE_CREDENTIALS, data['code'])

        if credentials is None:
            return self.json_message('Invalid code', status_code=400)

        await hass.auth.async_link_user(user, credentials)
        return self.json_message('User linked')


@callback
def _create_auth_code_store():
    """Create an in memory store."""
    temp_results = {}

    @callback
    def store_result(client_id, result):
        """Store flow result and return a code to retrieve it."""
        if isinstance(result, User):
            result_type = RESULT_TYPE_USER
        elif isinstance(result, Credentials):
            result_type = RESULT_TYPE_CREDENTIALS
        else:
            raise ValueError('result has to be either User or Credentials')

        code = uuid.uuid4().hex
        temp_results[(client_id, result_type, code)] = \
            (dt_util.utcnow(), result_type, result)
        return code

    @callback
    def retrieve_result(client_id, result_type, code):
        """Retrieve flow result."""
        key = (client_id, result_type, code)

        if key not in temp_results:
            return None

        created, _, result = temp_results.pop(key)

        # OAuth 4.2.1
        # The authorization code MUST expire shortly after it is issued to
        # mitigate the risk of leaks.  A maximum authorization code lifetime of
        # 10 minutes is RECOMMENDED.
        if dt_util.utcnow() - created < timedelta(minutes=10):
            return result

        return None

    return store_result, retrieve_result


@callback
def websocket_current_user(hass, connection, msg):
    """Return the current user."""
    user = connection.request.get('hass_user')

    if user is None:
        connection.to_write.put_nowait(websocket_api.error_message(
            msg['id'], 'no_user', 'Not authenticated as a user'))
        return

    connection.to_write.put_nowait(websocket_api.result_message(msg['id'], {
        'id': user.id,
        'name': user.name,
        'is_owner': user.is_owner,
        'credentials': [{'auth_provider_type': c.auth_provider_type,
                         'auth_provider_id': c.auth_provider_id}
                        for c in user.credentials]
    }))

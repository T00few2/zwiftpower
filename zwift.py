import requests
import backoff
import logging
import time
from requests.exceptions import RequestException

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('ZwiftAPI')

class ZwiftAPI:
    def __init__(self, username, password, client_id='Zwift Game Client'):
        self.username = username
        self.password = password
        self.client_id = client_id
        self.host = 'https://secure.zwift.com'
        self.token_url = f'{self.host}/auth/realms/zwift/protocol/openid-connect/token'
        self.auth_token = None
        self.refresh_token = None
        self.token_expiry_time = 0
    
    def authenticate(self):
        data = {
            'client_id': self.client_id,
            'grant_type': 'password',
            'username': self.username,
            'password': self.password
        }
        response = requests.post(self.token_url, data=data)
        if response.status_code == 200:
            self.auth_token = response.json()
            logger.info("Authenticated successfully.")
            logger.debug(f"Access token payload: {self.auth_token}")
        else:
            raise Exception(f"Authentication failed: {response.text}")

        expires_in = self.auth_token['expires_in']
        self.refresh_token = self.auth_token['refresh_token']
        logger.info(f"Token will expire in {expires_in} seconds.")
        # Calculate token expiry time (with 60-second buffer)
        self.token_expiry_time = time.time() + expires_in - 60
        
    def refresh_auth_token(self):
        logger.info("Refreshing auth token...")
        data = {
            'client_id': self.client_id,
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }
        response = requests.post(self.token_url, data=data)
        if response.status_code == 200:
            self.auth_token = response.json()
            logger.info("Token refreshed successfully.")
            # Update refresh token and expiry time when present
            self.refresh_token = self.auth_token.get('refresh_token', self.refresh_token)
            self.token_expiry_time = time.time() + self.auth_token.get('expires_in', 60) - 60
        else:
            logger.error(f"Token refresh failed: {response.text}")
            # If refresh fails, try to authenticate again
            logger.info("Attempting to re-authenticate...")
            self.authenticate()
    
    def ensure_valid_token(self):
        """Ensure the token is valid, refresh if needed."""
        if not self.is_authenticated():
            logger.info("Not authenticated. Authenticating now.")
            self.authenticate()
        elif time.time() >= self.token_expiry_time:
            logger.info("Token expired or about to expire. Refreshing...")
            self.refresh_auth_token()
    
    def is_authenticated(self):
        return self.auth_token is not None and 'access_token' in self.auth_token
    
    def fetch_json_with_retry(self, url, headers, params):
        """Fetch JSON data with retries and tolerant parsing.
        - Ensures Accept/User-Agent headers are present
        - Handles 204/empty body
        - Validates content-type before parsing JSON
        """
        req_headers = dict(headers or {})
        req_headers.setdefault('Accept', 'application/json, text/plain, */*')
        req_headers.setdefault('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')

        @backoff.on_exception(backoff.expo, (RequestException, ValueError), max_tries=5)
        def _fetch():
            response = requests.get(url, headers=req_headers, params=params, timeout=20)
            response.raise_for_status()

            # Handle no content / empty body
            if response.status_code == 204 or not response.content or not response.text.strip():
                return None

            content_type = (response.headers.get('Content-Type') or '').lower()
            text = response.text.strip()

            # Prefer JSON when content-type indicates JSON or payload looks like JSON
            if 'application/json' in content_type or text.startswith('{') or text.startswith('['):
                return response.json()

            # If not JSON, raise a descriptive error so caller can adjust
            snippet = text[:200]
            raise ValueError(f"Expected JSON but got Content-Type='{content_type}'. Body starts with: {snippet}")

        return _fetch()
            
    def get_profile(self, id):
        if not self.is_authenticated():
            raise Exception("Not authenticated. Please authenticate first.")
        
        headers = {
            'Authorization': f"Bearer {self.auth_token['access_token']}"
        }
  
        # Avoid double slash and use tolerant fetcher
        url = f'https://us-or-rly101.zwift.com/api/profiles/{id}'
        try:
            data = self.fetch_json_with_retry(url, headers=headers, params=None)
            return data or {}
        except requests.HTTPError as e:
            if getattr(e, 'response', None) is not None and e.response.status_code == 404:
                return None
            raise
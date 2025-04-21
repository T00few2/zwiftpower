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
            self.refresh_token = self.auth_token['refresh_token']
            # Calculate token expiry time (with 60-second buffer)
            self.token_expiry_time = time.time() + self.auth_token['expires_in'] - 60
            logger.info("Authenticated successfully.")
            logger.debug(f"Token will expire in {self.auth_token['expires_in']} seconds.")
        else:
            raise Exception(f"Authentication failed: {response.text}")
        
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
            self.refresh_token = self.auth_token['refresh_token']
            # Update token expiry time (with 60-second buffer)
            self.token_expiry_time = time.time() + self.auth_token['expires_in'] - 60
            logger.info("Token refreshed successfully.")
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
        """Fetch JSON data with retries, handling both request and JSON parsing errors."""
        @backoff.on_exception(backoff.expo, (RequestException, ValueError), max_tries=5)
        def _fetch():
            response = requests.get(url, headers=headers, params=params, timeout=20)
            response.raise_for_status()
            return response.json()
     
        return _fetch()
            
    def get_profile(self, id):
        # Ensure we have a valid token before making the request
        self.ensure_valid_token()
        
        headers = {
            'Authorization': f"Bearer {self.auth_token['access_token']}",
            'Content-Type': 'application/json'
        }
  
        url = f'https://us-or-rly101.zwift.com//api/profiles/{id}'
        try:
            return self.fetch_json_with_retry(url, headers=headers, params=None)
        except RequestException as e:
            if isinstance(e, requests.HTTPError) and e.response.status_code == 404:
                logger.warning(f"Profile with ID {id} not found")
                return None
            logger.error(f"Error fetching profile: {e}")
            raise
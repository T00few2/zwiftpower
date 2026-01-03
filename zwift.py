import requests
import backoff
import logging
import time
from requests.exceptions import RequestException
from typing import Any, Dict, List, Optional

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

    def get_club_roster(
        self,
        club_id: str,
        limit: int = 100,
        start: int = 0,
        paginate: bool = True,
        max_pages: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Fetch club roster (members) for a given club.

        Endpoint pattern:
          /api/clubs/club/{club_id}/roster?limit=100

        Some Zwift deployments paginate with `start`; some ignore it. We handle both.
        """
        if not self.is_authenticated():
            raise Exception("Not authenticated. Please authenticate first.")

        headers = {
            "Authorization": f"Bearer {self.auth_token['access_token']}",
            "Accept": "application/json",
        }

        url = f"https://us-or-rly101.zwift.com/api/clubs/club/{club_id}/roster"

        all_rows: List[Dict[str, Any]] = []
        cur_start = int(start)
        pages = 0

        while True:
            params: Dict[str, Any] = {"limit": int(limit)}
            if cur_start:
                params["start"] = cur_start

            data = self.fetch_json_with_retry(url, headers=headers, params=params)

            # Response shape can be a list or a wrapper dict depending on backend version.
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = (
                    data.get("roster")
                    or data.get("members")
                    or data.get("items")
                    or data.get("results")
                    or []
                )
            else:
                rows = []

            if rows:
                all_rows.extend(rows)

            pages += 1

            if not paginate:
                break

            # Stop when we get fewer than `limit` back (or no rows).
            if not rows or len(rows) < int(limit):
                break

            if pages >= int(max_pages):
                break

            cur_start += len(rows)

            # Be nice to the API.
            time.sleep(0.5)

        return all_rows

    @staticmethod
    def simplify_club_roster(roster: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Convert raw club roster entries into a minimal structure.

        Output fields:
          - firstName
          - lastName
          - gender
          - countryCode
          - profileId              (from membership.profileId, falls back to id)
          - createdOn              (top-level createdOn)
          - membershipCreatedOn    (membership.createdOn)
        """
        out: List[Dict[str, Any]] = []
        for m in roster or []:
            membership = (m or {}).get("membership") or {}
            out.append(
                {
                    "firstName": (m or {}).get("firstName"),
                    "lastName": (m or {}).get("lastName"),
                    "gender": (m or {}).get("gender"),
                    "countryCode": (m or {}).get("countryCode"),
                    "profileId": membership.get("profileId") or (m or {}).get("id"),
                    "createdOn": (m or {}).get("createdOn"),
                    "membershipCreatedOn": membership.get("createdOn"),
                }
            )
        return out
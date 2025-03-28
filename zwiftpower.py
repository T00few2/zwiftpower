import requests
from bs4 import BeautifulSoup
from collections import defaultdict
import html

class ZwiftPower:
    """
    A class to log into ZwiftPower, maintain an authenticated session,
    and fetch data from various ZwiftPower endpoints.
    """

    def __init__(self, username: str, password: str):
        """
        Initialize the ZwiftPower client. Credentials are saved and
        used during the login() flow.
        """
        self.username = username
        self.password = password
        self.session = requests.Session()
        # Spoof a common browser user agent
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 "
                "Safari/537.36"
            )
        })

    def login(self):
        """
        Performs the Zwift SSO login flow to authenticate on ZwiftPower.
        Updates self.session with the necessary cookies.
        """
        # 1) Hit ZwiftPower external login URL
        zwiftpower_login_url = (
            "https://zwiftpower.com/ucp.php?mode=login"
            "&login=external&oauth_service=oauthzpsso"
        )
        resp1 = self.session.get(zwiftpower_login_url, allow_redirects=False)
        if "Location" not in resp1.headers:
            raise RuntimeError("ZwiftPower login redirect not found.")

        # 2) Zwift SSO login page
        zwift_login_url = resp1.headers["Location"]
        resp2 = self.session.get(zwift_login_url, allow_redirects=False)

        # 3) Parse Zwift SSO form
        soup = BeautifulSoup(resp2.text, 'html.parser')
        form = soup.find('form', id='form')
        if not form or not form.get('action'):
            raise RuntimeError("Zwift login form not found or invalid.")

        action_url = form['action']  # the POST target
        payload = {
            tag['name']: tag.get('value', '')
            for tag in form.find_all('input') if tag.get('name')
        }
        payload['username'] = self.username
        payload['password'] = self.password

        if 'rememberMe' in payload:
            payload['rememberMe'] = 'on'

        # 4) POST credentials to Zwift
        resp3 = self.session.post(action_url, data=payload, allow_redirects=False)
        if "Location" not in resp3.headers:
            raise RuntimeError("Zwift login credentials likely incorrect or 2FA needed.")

        # 5) Final redirect to ZwiftPower (sets final ZwiftPower cookie)
        final_url = resp3.headers["Location"]
        resp4 = self.session.get(final_url, allow_redirects=True)

        # If we want, we can confirm by checking ZwiftPower HTML or cookies
        # for proof we're logged in. For brevity, just check status:
        if resp4.status_code != 200:
            raise RuntimeError(
                f"ZwiftPower final login redirect failed (status={resp4.status_code})"
            )

    def get_team_riders(self, club_id: int) -> dict:
        """
        Fetch JSON data about the riders in a given team/club ID.
        Returns the parsed JSON as a dictionary.
        """
        url = f"https://zwiftpower.com/api3.php?do=team_riders&id={club_id}"
        resp = self.session.get(url)
        resp.raise_for_status()  # Raise an exception for non-200
        return resp.json()

    def get_team_results(self, club_id: int) -> dict:
        """
        Fetch JSON data about the team's results for a given team/club ID.
        Returns the parsed JSON as a dictionary.
        """
        url = f"https://zwiftpower.com/api3.php?do=team_results&id={club_id}"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def get_rider_data_json(self, rider_id: int) -> dict:
        """
        Fetch the rider's JSON from the "cache3/profile" endpoint,
        e.g., /cache3/profile/<rider_id>_all.json
        """
        url = f"https://zwiftpower.com/cache3/profile/{rider_id}_all.json"
        resp = self.session.get(url)
        if resp.status_code == 200:
            return resp.json()
        else:
            # Could raise an exception or return an empty dict
            return {}

    def get_rider_zrs(self, rider_id: int) -> str:
        """
        Fetch the Zwift Racing Score by scraping the HTML on
        /profile.php?z=<rider_id>. Returns the score as a string,
        or None if not found.
        """
        url = f"https://zwiftpower.com/profile.php?z={rider_id}"
        resp = self.session.get(url)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Locate the <th> with "Zwift Racing Score", then get next <td> <b>
        racing_score_th = soup.find("th", string=lambda x: x and "Zwift Racing Score" in x)
        if racing_score_th:
            score_td = racing_score_th.find_next("td")
            if score_td and score_td.find("b"):
                return score_td.find("b").get_text(strip=True)
        return None
    
    def analyze_team_results(self, team_results: dict) -> dict:
        """
        Given a dict from get_team_results (containing 'events' and 'data'),
        produce three analyses:
          1) top 10 events (by zid) with participants,
          2) top 10 events (by event title) aggregated,
          3) a list of riders with the most top-3 positions_in_cat.

        Returns a dict with three keys:
          {
            "top_10_by_zid": [...],
            "top_10_by_title": [...],
            "top_3_riders": [...]
          }
        }
        """
        # ===========================
        # 1) Top 10 events BY ZID
        # ===========================
        events = team_results["events"]
        rows = team_results["data"]

        # Group riders by zid
        by_event = defaultdict(list)
        for row in rows:
            # Use html.unescape() for rider names
            rider_name = html.unescape(row["name"])

            new_row = dict(row)  # copy to avoid mutating original
            new_row["name"] = rider_name

            zid = new_row["zid"]
            by_event[zid].append(new_row)

        # Count how many riders each zid has
        event_counts = [(zid, len(riders)) for zid, riders in by_event.items()]
        # Sort descending by participant count
        event_counts.sort(key=lambda x: x[1], reverse=True)
        # Take top 10
        top_zids = event_counts[:10]

        top_10_by_zid = []
        for zid, rider_count in top_zids:
            event_info = events.get(zid, {})
            event_title = event_info.get("title", f"(No title for {zid})")
            riders_for_this_zid = by_event[zid]

            # Build participant list
            participants = []
            for r in riders_for_this_zid:
                participants.append({
                    "name": r["name"],
                    "zwid": r["zwid"]
                })

            top_10_by_zid.append({
                "zid": zid,
                "title": event_title,
                "rider_count": rider_count,
                "participants": participants
            })

        # ===========================
        # 2) Top 10 events BY TITLE
        # ===========================
        title_counts = defaultdict(int)

        for row in rows:
            zid = row["zid"]
            evt_obj = events.get(zid, {})
            raw_title = evt_obj.get("title", "(No title)")
            event_title = html.unescape(raw_title)
            title_counts[event_title] += 1

        # Sort descending
        sorted_by_count = sorted(title_counts.items(), key=lambda x: x[1], reverse=True)
        # Take top 10
        sorted_by_count = sorted_by_count[:10]

        top_10_by_title = []
        for title, count in sorted_by_count:
            top_10_by_title.append({
                "event_name": title,
                "participant_count": count
            })

        # ===========================
        # 3) Riders with the MOST top 3 FINISHES in their category
        # ===========================
        top_3_counter = defaultdict(int)
        for row in rows:
            pos_in_cat = row.get("position_in_cat")
            if pos_in_cat is not None and pos_in_cat <= 3:
                # Convert name
                rider_name = html.unescape(row["name"])
                zwid = row["zwid"]
                # Could key by just zwid or by (rider_name, zwid)
                key = (rider_name, zwid)
                top_3_counter[key] += 1

        # Sort by number of top-3 finishes, descending
        sorted_top_3 = sorted(top_3_counter.items(), key=lambda x: x[1], reverse=True)

        top_3_riders = []
        for (name, zwid), count in sorted_top_3:
            top_3_riders.append({
                "name": name,
                "zwid": zwid,
                "top_3_count": count
            })

        # ===========================
        # Return all three analyses in a dict
        # ===========================
        return {
            "top_10_by_zid": top_10_by_zid,
            "top_10_by_title": top_10_by_title,
            "top_3_riders": top_3_riders
        }
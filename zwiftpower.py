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
          4) a list of winners (position_in_cat == 1)
          5) a list of top riders by wkg1200 (20-minute power)
          6) a list of top riders by wkg300 (5-minute power)
          7) a list of top riders by wkg60 (1-minute power)

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
            new_row["position_in_cat"] = row.get("position_in_cat")

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
                    "zwid": r["zwid"],
                    #"position_in_event": r["position_in_cat"]
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
            type = row.get("f_t")
            if pos_in_cat is not None and pos_in_cat <= 3 and type == 'TYPE_RACE ':
                # Convert name
                rider_name = html.unescape(row["name"])
                zwid = row["zwid"]
                # Could key by just zwid or by (rider_name, zwid)
                key = (rider_name, zwid)
                top_3_counter[key] += 1

        # Sort by number of top-3 finishes, descending
        sorted_top_3 = sorted(top_3_counter.items(), key=lambda x: x[1], reverse=True)[:3]

        top_3_riders = []
        for (name, zwid), count in sorted_top_3:
            top_3_riders.append({
                "name": name,
                "zwid": zwid,
                "top_3_count": count
            })
        
        # ===========================
        # 4) Winners (position_in_cat == 1)
        # ===========================
        winners = []
        for row in rows:
            pos_in_cat = row.get("position_in_cat")
            type = row.get("f_t")
            if pos_in_cat is not None and pos_in_cat <= 1 and type == 'TYPE_RACE ':
                # Convert name
                rider_name = html.unescape(row["name"])
                zwid = row["zwid"]
                zid = row["zid"]
                evt_obj = events.get(zid, {})
                raw_title = evt_obj.get("title", "(No title)")
                event_title = html.unescape(raw_title)
                winners.append({
                    "name": rider_name,
                    "zwid": zwid,
                    "event_title": event_title
                })
                
        # ==============================================================
        # 5) Top riders by wkg1200 (20-minute power)
        # 6) Top riders by wkg300  (5-minute  power)
        # ==============================================================
        # We’ll track each rider’s best 20-min and 5-min stats:
        # best_20min[(rider_name, zwid)] = {"value": float, "event_title": str}
        # best_5min [(rider_name, zwid)] = {"value": float, "event_title": str}
        best_20min = defaultdict(lambda: {"value": 0.0, "event_title": None})
        best_5min  = defaultdict(lambda: {"value": 0.0, "event_title": None})
        best_1min  = defaultdict(lambda: {"value": 0.0, "event_title": None})

        for row in rows:
            rider_name = html.unescape(row["name"])
            zwid = row["zwid"]
            rider_id = (rider_name, zwid)

            zid = row["zid"]
            event_info = events.get(zid, {})
            # If you want to unescape the event's title just in case
            raw_title = event_info.get("title", f"(No title for {zid})")
            event_title = html.unescape(raw_title)
            
            wgk60_raw = row.get("wkg60")
            pos_in_cat = row.get("position_in_cat")
            if isinstance(wgk60_raw, list) and len(wgk60_raw) > 0:
                wkg60_str = wgk60_raw[0]
                # If it's numeric, parse to float
                if isinstance(wkg60_str, str) and wkg60_str.replace('.', '', 1).isdigit():
                    val_60 = float(wkg60_str)
                    # If higher than our current best, update
                    if val_60 > best_1min[rider_id]["value"]:
                        best_1min[rider_id]["value"] = val_60
                        best_1min[rider_id]["event_title"] = event_title
                        best_1min[rider_id]["position_in_cat"] = pos_in_cat

            # row["wkg1200"] looks like ['3.2', 0] if using your data structure
            wkg1200_raw = row.get("wkg1200")
            pos_in_cat = row.get("position_in_cat")
            if isinstance(wkg1200_raw, list) and len(wkg1200_raw) > 0:
                wkg1200_str = wkg1200_raw[0]  # e.g. "3.2"
                # If it's numeric, parse to float
                if isinstance(wkg1200_str, str) and wkg1200_str.replace('.', '', 1).isdigit():
                    val_1200 = float(wkg1200_str)
                    # If higher than our current best, update
                    if val_1200 > best_20min[rider_id]["value"]:
                        best_20min[rider_id]["value"] = val_1200
                        best_20min[rider_id]["event_title"] = event_title
                        best_20min[rider_id]["position_in_cat"] = pos_in_cat

            # Same logic for wkg300 (5-min)
            wkg300_raw = row.get("wkg300")
            pos_in_cat = row.get("position_in_cat")
            if isinstance(wkg300_raw, list) and len(wkg300_raw) > 0:
                wkg300_str = wkg300_raw[0]
                if isinstance(wkg300_str, str) and wkg300_str.replace('.', '', 1).isdigit():
                    val_300 = float(wkg300_str)
                    if val_300 > best_5min[rider_id]["value"]:
                        best_5min[rider_id]["value"] = val_300
                        best_5min[rider_id]["event_title"] = event_title
                        best_5min[rider_id]["position_in_cat"] = pos_in_cat

        # Now we can sort them descending by the "value"
        sorted_20min = sorted(
            best_20min.items(),
            key=lambda x: x[1]["value"],
            reverse=True
        )[:3]

        sorted_5min = sorted(
            best_5min.items(),
            key=lambda x: x[1]["value"],
            reverse=True
        )[:3]
        
        sorted_1min = sorted(
            best_1min.items(),
            key=lambda x: x[1]["value"],
            reverse=True
        )[:3]

        # Build final lists
        top_wkg1200 = []
        for (name, zwid), info in sorted_20min:
            top_wkg1200.append({
                "name": name,
                "zwid": zwid,
                "wkg1200": info["value"],
                "event_title": info["event_title"],
                "position_in_cat": info["position_in_cat"]
            })

        top_wkg300 = []
        for (name, zwid), info in sorted_5min:
            top_wkg300.append({
                "name": name,
                "zwid": zwid,
                "wkg300": info["value"],
                "event_title": info["event_title"],
                "position_in_cat": info["position_in_cat"]
            })
        
        top_wkg60 = []
        for (name, zwid), info in sorted_1min:
            top_wkg60.append({
                "name": name,
                "zwid": zwid,
                "wkg60": info["value"],
                "event_title": info["event_title"],
                "position_in_cat": info["position_in_cat"]
            })
        
        # ===========================
        # 7) The three riders with the most completed events
        # ===========================
        event_counter = defaultdict(int)
        for row in rows:
            # Convert name
            rider_name = html.unescape(row["name"])
            zwid = row["zwid"]
            # Could key by just zwid or by (rider_name, zwid)
            key = (rider_name, zwid)
            event_counter[key] += 1

        # Sort by number of top-3 finishes, descending
        sorted_most_events = sorted(event_counter.items(), key=lambda x: x[1], reverse=True)[:3]

        most_event_riders = []
        for (name, zwid), count in sorted_most_events:
            most_event_riders.append({
                "name": name,
                "zwid": zwid,
                "events_count": count
            })

        # ===========================
        # Return all three analyses in a dict
        # ===========================
        return {
            "top_10_by_zid": top_10_by_zid, # top 10 events with most participants by race id
            "top_10_by_title": top_10_by_title, # top 10 events with most participants by event title (can be across multiple races)
            "most_events_riders": most_event_riders, # top 3 riders with most completed events
            "most_top_3_riders": top_3_riders, # top 3 riders with most top-3 finishes in their category
            "winners": winners, # list of winners in races
            "top_watts_per_kg_20min": top_wkg1200, # top riders by 20-minute power
            "top_watts_per_kg_5min": top_wkg300, # top riders by 5-minute power
            "top_watts_per_kg_1min": top_wkg60 # top riders by 1-minute power
        }
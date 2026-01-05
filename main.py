import os
import time
import inspect
import logging
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, flash
from dotenv import load_dotenv
from functools import wraps
import requests
from datetime import datetime, timedelta
import firebase
from discord_api import DiscordAPI
from zwift import ZwiftAPI
import pytz
from bisect import bisect_right
from typing import Optional
from zwiftpower import ZwiftPower
from zwiftcommentator import ZwiftCommentator

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Configure session
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key-change-this-in-production")

# Discord OAuth Configuration
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "your_discord_client_id")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "your_discord_client_secret")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8080/auth/discord/callback")
DISCORD_OAUTH_URL = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify%20guilds"

@app.before_request
def log_request_path():
    logging.info({
        "endpoint": request.path,
        "method": request.method,
        "query": request.args.to_dict(),
        "user_agent": request.headers.get('User-Agent', ''),
        "remote_addr": request.remote_addr,
        "timestamp": time.time()
    })

# Global variables to cache authenticated sessions
cached_session = None
cached_session_timestamp = None 
cached_zwift_api = None
cached_zwift_api_timestamp = None
SESSION_VALIDITY = 3600  # seconds (how long the session is expected to be valid)

ZWIFT_USERNAME = os.getenv("ZWIFT_USERNAME", "your_username")
ZWIFT_PASSWORD = os.getenv("ZWIFT_PASSWORD", "your_password")

OPENAI_KEY = os.getenv("OPENAI_KEY", "your_openai_key")

DISCORD_GOSSIP_ID = os.getenv("DISCORD_GOSSIP_ID", "your_discord_gossip_id")
DISCORD_BOT_URL = os.getenv("DISCORD_BOT_URL", "your_discord_bot_url")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "your_discord_bot_token")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "your_discord_guild_id")

# Role IDs for filtering community/verified members (can be overridden via environment)
COMMUNITY_MEMBER_ROLE_ID = os.getenv(
    "DISCORD_COMMUNITY_MEMBER_ROLE_ID", "1195878123795910736"
)
VERIFIED_MEMBER_ROLE_ID = os.getenv(
    "DISCORD_VERIFIED_MEMBER_ROLE_ID", "1385216556166025347"
)

# Cache for verified Zwift IDs (to avoid enumerating all Discord members on every cron hit)
_verified_zwift_ids_cache: Optional[set[str]] = None
_verified_zwift_ids_cache_ts: Optional[float] = None
VERIFIED_ZWIFT_IDS_CACHE_TTL_SECONDS = int(os.getenv("VERIFIED_ZWIFT_IDS_CACHE_TTL_SECONDS", "900"))  # 15 min


def _get_verified_member_zwift_ids(force_refresh: bool = False) -> set[str]:
    """
    Return a set of Zwift IDs for Discord members who currently have the Verified Member role.

    Source of truth:
      - Discord guild member roles (role_ids)
      - Firestore users collection for discordId -> zwiftId link
    """
    global _verified_zwift_ids_cache, _verified_zwift_ids_cache_ts

    now = time.time()
    if (not force_refresh
        and _verified_zwift_ids_cache is not None
        and _verified_zwift_ids_cache_ts is not None
        and (now - _verified_zwift_ids_cache_ts) < VERIFIED_ZWIFT_IDS_CACHE_TTL_SECONDS):
        return _verified_zwift_ids_cache

    role_id = str(VERIFIED_MEMBER_ROLE_ID or "").strip()
    if not role_id:
        _verified_zwift_ids_cache = set()
        _verified_zwift_ids_cache_ts = now
        return _verified_zwift_ids_cache

    guild_id = os.environ.get("DISCORD_GUILD_ID")
    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    if not guild_id or not bot_token:
        # Don't hard-fail cron endpoints if env is misconfigured; just return empty allowlist.
        _verified_zwift_ids_cache = set()
        _verified_zwift_ids_cache_ts = now
        return _verified_zwift_ids_cache

    # Build discordId -> zwiftId lookup from Firestore users
    discord_to_zwift: dict[str, str] = {}
    try:
        users = firebase.get_collection("users", limit=100000) or []
        for u in users:
            if not isinstance(u, dict):
                continue
            did = u.get("discordId")
            zid = u.get("zwiftId")
            did_s = str(did).strip() if did is not None else ""
            zid_s = str(zid).strip() if zid is not None else ""
            if did_s and zid_s:
                discord_to_zwift[did_s] = zid_s
    except Exception as e:
        print(f"[WARN] Failed to load users for verified allowlist: {e}")
        discord_to_zwift = {}

    verified_zwift_ids: set[str] = set()
    try:
        discord_api = DiscordAPI(bot_token, guild_id)
        members = discord_api.get_all_members(limit=100000, include_role_names=False) or []
        for m in members:
            if not isinstance(m, dict):
                continue
            discord_id = str(m.get("discordID") or "").strip()
            if not discord_id:
                continue
            role_ids = [str(r) for r in (m.get("role_ids") or [])]
            if role_id in role_ids:
                zwift_id = discord_to_zwift.get(discord_id)
                if zwift_id:
                    verified_zwift_ids.add(str(zwift_id).strip())
    except Exception as e:
        print(f"[WARN] Failed to build verified allowlist from Discord: {e}")
        verified_zwift_ids = set()

    _verified_zwift_ids_cache = verified_zwift_ids
    _verified_zwift_ids_cache_ts = now
    return verified_zwift_ids

CONTENT_API_KEY = os.getenv("CONTENT_API_KEY", "your_content_api_key")
ZWIFT_CLUB_ID = os.getenv("ZWIFT_CLUB_ID", "")  # Optional default for roster refresh
ZWIFTPOWER_CLUB_ID = os.getenv("ZWIFTPOWER_CLUB_ID", "")  # ZwiftPower team/club id for roster refresh (required)

def login_required(f):
    """Decorator to require Discord OAuth login with admin rights"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session or 'discord_id' not in session:
            # Store the URL they were trying to access
            session['next_url'] = request.url
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def check_discord_admin(discord_id, access_token):
    """Check if user has admin rights in the Discord server"""
    try:
        # Get user's guilds
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get('https://discord.com/api/users/@me/guilds', headers=headers)
        if response.status_code != 200:
            return False
        
        guilds = response.json()
        
        # Check if user is in our guild and has admin permissions
        for guild in guilds:
            if guild['id'] == DISCORD_GUILD_ID:
                # Check for admin permissions (0x8 = ADMINISTRATOR)
                permissions = int(guild.get('permissions', 0))
                has_admin = (permissions & 0x8) == 0x8
                return has_admin
        
        return False
    except Exception as e:
        print(f"Error checking Discord admin status: {e}")
        return False

def verify_api_key():
    """Verify API key from Authorization header"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return False
    
    token = auth_header.replace('Bearer ', '')
    return token == CONTENT_API_KEY


def _commit_in_batches(write_ops, batch_size: int = 450):
    """
    Commit Firestore batch writes in chunks (Firestore limit is 500 ops per batch).
    write_ops: iterable of callables that accept a firestore batch.
    """
    ops = list(write_ops or [])
    committed = 0
    for i in range(0, len(ops), batch_size):
        batch = firebase.db.batch()
        chunk = ops[i:i + batch_size]
        for op in chunk:
            op(batch)
        batch.commit()
        committed += len(chunk)
    return committed


def overwrite_companion_club_members_in_firestore(members: list[dict]) -> dict:
    """
    Store the current club roster in Firestore as an "official membership list",
    overwriting any previous data.

    Layout:
      - companion_club_members/{profileId} (per-member docs)

    Each member doc gets:
      - rosterSyncedAt: datetime
    """
    sync_ts = datetime.utcnow()

    members_col_ref = firebase.db.collection("companion_club_members")

    # 1) Delete all existing docs (full overwrite)
    existing_stream = members_col_ref.stream()

    def make_delete_op(doc_ref):
        def _op(batch):
            batch.delete(doc_ref)
        return _op

    delete_ops = [make_delete_op(doc.reference) for doc in existing_stream]
    deleted_count = _commit_in_batches(delete_ops)

    # Upsert members
    def make_set_op(profile_id: str, data: dict):
        doc_ref = members_col_ref.document(str(profile_id))

        def _op(batch):
            batch.set(
                doc_ref,
                {
                    **(data or {}),
                    "profileId": str(profile_id),
                    "rosterSyncedAt": sync_ts,
                    "updatedAt": sync_ts,
                },
                merge=False,
            )

        return _op

    upsert_ops = []
    for m in members or []:
        pid = (m or {}).get("profileId")
        if pid is None:
            continue
        upsert_ops.append(make_set_op(str(pid), m))

    upserted_count = _commit_in_batches(upsert_ops)

    return {
        "memberCount": len(members or []),
        "syncedAt": sync_ts.isoformat() + "Z",
        "deleted": deleted_count,
        "upserted": upserted_count,
    }


def overwrite_zwiftpower_club_members_in_firestore(members: list[dict]) -> dict:
    """
    Store ZwiftPower team_riders roster in Firestore, overwriting any previous data.

    Collection:
      - zwiftpower_club_members/{zwid}

    Fields stored per doc:
      - zwid (Zwift ID)
      - name
      - rank (numeric when possible)
      - rankRaw (original rank value)
      - rosterSyncedAt, updatedAt
    """
    sync_ts = datetime.utcnow()
    col_ref = firebase.db.collection("zwiftpower_club_members")

    # 1) Delete all existing docs (full overwrite)
    existing_stream = col_ref.stream()

    def make_delete_op(doc_ref):
        def _op(batch):
            batch.delete(doc_ref)
        return _op

    delete_ops = [make_delete_op(doc.reference) for doc in existing_stream]
    deleted_count = _commit_in_batches(delete_ops)

    # 2) Write fresh docs keyed by zwid
    def make_set_op(doc_id: str, data: dict):
        doc_ref = col_ref.document(str(doc_id))

        def _op(batch):
            batch.set(
                doc_ref,
                {
                    **(data or {}),
                    "rosterSyncedAt": sync_ts,
                    "updatedAt": sync_ts,
                },
                merge=False,
            )

        return _op

    upsert_ops = []
    for m in members or []:
        zwid = (m or {}).get("zwid")
        if zwid is None:
            continue
        upsert_ops.append(make_set_op(str(zwid), m))

    upserted_count = _commit_in_batches(upsert_ops)

    return {
        "memberCount": len(members or []),
        "syncedAt": sync_ts.isoformat() + "Z",
        "deleted": deleted_count,
        "upserted": upserted_count,
    }


def _refresh_companion_club_roster(club_id: str, limit: int = 100, paginate: bool = True) -> dict:
    """Fetch roster from Zwift and overwrite Firestore collection companion_club_members."""
    zwift_api = get_authenticated_zwift_api()
    zwift_api.ensure_valid_token()

    roster = zwift_api.get_club_roster(str(club_id), limit=limit, paginate=paginate)
    simplified = ZwiftAPI.simplify_club_roster(roster or [])

    result = overwrite_companion_club_members_in_firestore(simplified)
    return {
        "status": "success",
        "clubId": str(club_id),
        "fetched": len(roster or []),
        "stored": result["memberCount"],
        "deleted": result["deleted"],
        "upserted": result["upserted"],
        "syncedAt": result["syncedAt"],
    }

def get_authenticated_session() -> requests.Session:
    """Return a cached, authenticated session if available and still valid; otherwise, log in."""
    global cached_session, cached_session_timestamp
    now = time.time()
    # If we have a session and it hasn't expired yet, reuse it.
    if cached_session and cached_session_timestamp and (now - cached_session_timestamp < SESSION_VALIDITY):
        print("Using cached authenticated session (container is warm).")
        return cached_session

    # Otherwise, create a new session and log in.
    print("No valid session found. Logging in again.")
    new_session = requests.Session()
    new_session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 "
            "Safari/537.36"
        )
    })

    zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
    zp.session = new_session  # Use our new session
    zp.login()
    # Update the cache
    cached_session = new_session
    cached_session_timestamp = now
    return new_session

def get_authenticated_zwift_api() -> ZwiftAPI:
    """Return a cached, authenticated ZwiftAPI if available and still valid; otherwise, create a new one."""
    global cached_zwift_api, cached_zwift_api_timestamp
    now = time.time()
    
    # If we have a valid cached API instance, reuse it
    if cached_zwift_api and cached_zwift_api_timestamp and (now - cached_zwift_api_timestamp < SESSION_VALIDITY):
        print("Using cached authenticated ZwiftAPI (container is warm).")
        # Ensure token is still valid
        cached_zwift_api.ensure_valid_token()
        return cached_zwift_api

    # Otherwise, create a new API instance and authenticate
    print("No valid ZwiftAPI found. Creating and authenticating new instance.")
    new_zwift_api = ZwiftAPI(ZWIFT_USERNAME, ZWIFT_PASSWORD)
    new_zwift_api.authenticate()
    
    # Update the cache
    cached_zwift_api = new_zwift_api
    cached_zwift_api_timestamp = now
    return new_zwift_api

@app.route('/team_riders/<int:club_id>', methods=['GET'])
def team_riders(club_id: int):
    """Get ZwiftPower registered team riders for a given club ID"""
    try:
        session = get_authenticated_session()
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.session = session
        data = zp.get_team_riders(club_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/team_results/<int:club_id>', methods=['GET'])
def team_results(club_id: int):
    """Get ZwiftPower weekly team results for a given club ID"""
    try:
        session = get_authenticated_session()
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.session = session
        data = zp.get_team_results(club_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/filter_events/<int:club_id>', methods=['GET'])
def filter_events(club_id: int):
    """
    Filter team events by title pattern.
    
    Query Parameters:
        title (str): The pattern to match in event titles (case-insensitive)
        
    Example:
        /filter_events/11939?title=Tour%20de%20Zwift
    """
    try:
        # Get the title pattern from query parameters
        title_pattern = request.args.get('title')
        if not title_pattern:
            return jsonify({"error": "Missing 'title' query parameter"}), 400

        # Get authenticated session and create ZwiftPower instance
        session = get_authenticated_session()
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.session = session

        # Filter events by title
        filtered_data = zp.filter_events_by_title(club_id, title_pattern)
        
        # If no events found, return appropriate message
        if not filtered_data:
            return jsonify({
                "message": f"No events found matching pattern '{title_pattern}'",
                "filtered_events": {}
            })

        return jsonify({
            "message": f"Found {len(filtered_data)} events matching pattern '{title_pattern}'",
            "filtered_events": filtered_data
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/rider_data/<int:rider_id>', methods=['GET'])
def rider_data(rider_id: int):
    """Get ZwiftPowerrider data for a given rider ID"""
    try:
        session = get_authenticated_session()
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.session = session
        data = zp.get_rider_data_json(rider_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/generate_and_post_commentary/<int:club_id>', methods=['POST'])
def generate_and_post_commentary(club_id):
    """Generate weekly commentary on team results and post to Discord"""
    try:
        print(f"[DEBUG] Starting commentary generation for club ID: {club_id}")

        session = get_authenticated_session()
        print("[DEBUG] Authenticated session established")

        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.session = session
        allowed_zwids = _get_verified_member_zwift_ids()
        results = zp.get_team_results(club_id, allowed_zwids=allowed_zwids)
        results_summary = zp.analyze_team_results(results)

        print("[DEBUG] Team results fetched:", results)

        if not results:
            return jsonify({"error": "No results found"}), 404

        commentator = ZwiftCommentator(api_key=OPENAI_KEY)
        commentary = commentator.generate_commentary(results_summary)

        print("[DEBUG] Commentary generated:\n", commentary)

        response = commentator.send_to_discord_api(
            channel_id=DISCORD_GOSSIP_ID,
            message=commentary,
            api_url=DISCORD_BOT_URL
        )

        print("[DEBUG] Discord response:", response)

        if response and response.get("success"):
            return jsonify({"success": True, "message": commentary})
        else:
            return jsonify({"error": "Failed to send to Discord", "details": response}), 500

    except Exception as e:
        print("[ERROR] Exception occurred:", e)
        return jsonify({"error": str(e)}), 500
    
@app.route('/generate_and_post_upgrades', methods=['POST'])
def generate_and_post_upgrades():
    """Generate commentary on upgrades and post to Discord"""
    try:
        print("[DEBUG] Starting upgrade comment generation...")

        today = datetime.now().strftime("%y%m%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%y%m%d")

        # Use firebase.compare_rider_categories instead of external API
        print(f"[DEBUG] Comparing rider categories between {today} and {yesterday}")
        allowed_zwids = _get_verified_member_zwift_ids()
        upgrade_data = firebase.compare_rider_categories(today, yesterday, allowed_rider_ids=allowed_zwids)

        # Be defensive: comparison should return a dict, but don't fail cron runs if it doesn't.
        if not isinstance(upgrade_data, dict):
            print(f"[WARN] compare_rider_categories returned non-dict: {type(upgrade_data)}")
            return jsonify({"message": "No upgrades today."}), 200

        if (not upgrade_data.get("upgradedZPCategory")
            and not upgrade_data.get("upgradedZwiftRacingCategory")
            and not upgrade_data.get("upgradedZRSCategory")):
            return jsonify({"message": "No upgrades today."}), 200

        # Generate upgrade comment
        commentator = ZwiftCommentator(api_key=OPENAI_KEY)
        comment = commentator.generate_upgrade_comment(upgrade_data)

        # Post comment to Discord
        discord_response = commentator.send_to_discord_api(
            channel_id=DISCORD_GOSSIP_ID,
            message=comment,
            api_url=DISCORD_BOT_URL
        )

        if discord_response and discord_response.get("success"):
            return jsonify({"success": True, "message": comment})
        else:
            return jsonify({"error": "Failed to send to Discord", "details": discord_response}), 500

    except Exception as e:
        print("[ERROR] Exception occurred:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/users', methods=['GET'])
@app.route('/discord_users', methods=['GET'])  # Keep old route for backwards compatibility
def get_users():
    """Retrieve all users from Firebase"""
    try:
        # Get limit parameter from query string, default to 100
        limit = request.args.get('limit', default=100, type=int)
        
        # Call the get_collection function from firebase module
        users = firebase.get_collection('users', limit=limit)
        
        # Return the users as JSON
        return jsonify({"users": users, "count": len(users)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/discord/members', methods=['GET'])
@login_required
def get_discord_members():
    """Webapplication to assign ZwiftIDs to Discord users"""
    try:
        # Check if the request accepts HTML
        is_html_request = request.headers.get('Accept', '').find('text/html') >= 0
        
        # Initialize Discord API
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        
        # Get parameters
        member_type = request.args.get('type', default='all')
        include_roles = request.args.get('include_roles', default='true').lower() == 'true'
        
        if member_type == 'linked':
            # Only get members with ZwiftIDs
            members = discord_api.find_linked_members(include_role_names=include_roles)
        elif member_type == 'unlinked':
            # Only get members without ZwiftIDs
            members = discord_api.find_unlinked_members(include_role_names=include_roles)
        else:
            # Get all members with ZwiftIDs merged
            members = discord_api.merge_with_zwift_ids(include_role_names=include_roles)

        # Compute "member role" flag using role ID (robust vs role name changes)
        # Requirement: only check the Community Member role id (1195878123795910736 by default).
        community_role_id = str(COMMUNITY_MEMBER_ROLE_ID or "").strip()
        for m in members:
            role_ids = [str(r) for r in (m.get("role_ids") or [])]
            m["has_member_role"] = bool(community_role_id and community_role_id in role_ids)

        # Compute companion membership flag by checking companion_club_members/{profileId}
        companion_ids = set()
        try:
            companion_ids = set(
                [doc.id for doc in firebase.db.collection("companion_club_members").stream()]
            )
        except Exception as comp_err:
            print(f"Warning: could not load companion_club_members: {comp_err}")
            companion_ids = set()

        # Compute ZwiftPower roster membership flag by checking zwiftpower_club_members/{zwid}
        zwiftpower_ids = set()
        try:
            zwiftpower_ids = set(
                [doc.id for doc in firebase.db.collection("zwiftpower_club_members").stream()]
            )
        except Exception as zp_err:
            print(f"Warning: could not load zwiftpower_club_members: {zp_err}")
            zwiftpower_ids = set()

        for m in members:
            zwift_id = m.get("zwiftID")
            m["is_companion_member"] = bool(
                zwift_id is not None and str(zwift_id) in companion_ids
            )
            m["is_zwiftpower_member"] = bool(
                zwift_id is not None and str(zwift_id) in zwiftpower_ids
            )
            
        # For HTML requests, render the template with data
        if is_html_request:
            # Get Zwift riders from club_riders collection
            club_riders_data = firebase.get_latest_document('club_stats')
            zwift_riders = []
            
            if club_riders_data and len(club_riders_data) > 0:
                # The data comes back as a list with one item, get the first item
                riders = club_riders_data[0]['data'].get('riders', [])
                
                # Debug the structure
                print(f"Found {len(riders)} riders in club_riders data")
                if riders:
                    print(f"Sample rider data: {riders[0].keys()}")
                    print(f"Sample rider name: {riders[0].get('name', 'NO_NAME')}")
                    print(f"Sample rider ID: {riders[0].get('riderId', 'NO_ID')}")
                
                zwift_riders = [
                    {"name": rider.get('name', ''), "riderId": str(rider.get('riderId', ''))}
                    for rider in riders
                    if 'name' in rider and 'riderId' in rider
                ]
                
                # Sort riders by name for easier selection
                zwift_riders.sort(key=lambda x: x["name"])
                
                # Debug the processed riders
                print(f"Processed {len(zwift_riders)} riders for dropdown")
            else:
                print("No club_riders data found or data is empty")
            
            linked_count = len([m for m in members if m.get('has_zwift_id')])
            unlinked_count = len(members) - linked_count
            member_role_count = len([m for m in members if m.get("has_member_role")])
            companion_count = len([m for m in members if m.get("is_companion_member")])
            zwiftpower_count = len([m for m in members if m.get("is_zwiftpower_member")])
            
            return render_template(
                'discord_members.html',
                members=members,
                zwift_riders=zwift_riders,
                linked_count=linked_count,
                unlinked_count=unlinked_count,
                member_role_count=member_role_count,
                companion_count=companion_count,
                zwiftpower_count=zwiftpower_count,
            )
        
        # For API requests, return JSON
        return jsonify({
            "members": members,
            "count": len(members),
            "type": member_type,
            "include_roles": include_roles
        })
    except ValueError as e:
        if is_html_request:
            return f"<h1>Error</h1><p>{str(e)}</p>", 400
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        if is_html_request:
            return f"<h1>Error</h1><p>{str(e)}</p>", 500
        return jsonify({"error": str(e)}), 500


@app.route('/discord/member-outreach', methods=['GET'])
@login_required
def member_outreach_view():
    """
    Admin view: Member outreach via DM, currently focused on community members without linked Zwift IDs.
    """
    try:
        # Only render HTML for now (no JSON API needed yet)
        is_html_request = request.headers.get('Accept', '').find('text/html') >= 0

        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)

        # Start from all members without Zwift IDs
        members = discord_api.find_unlinked_members(include_role_names=True)

        # Filter to community members (have community role, do NOT have verified role)
        community_role_id = COMMUNITY_MEMBER_ROLE_ID
        verified_role_id = VERIFIED_MEMBER_ROLE_ID

        filtered_members = []
        for m in members:
            role_ids = m.get("role_ids", [])
            if community_role_id in role_ids and verified_role_id not in role_ids:
                filtered_members.append(m)

        # Load reminder metadata from Firestore
        reminder_docs = firebase.get_collection(
            "discord_zwift_reminders", limit=10000, include_id=True
        )
        reminder_lookup = {doc.get("id"): doc for doc in reminder_docs}

        # Attach reminder info to members
        from datetime import datetime

        def format_ts(ts):
            if not ts:
                return None, None
            # Firestore returns datetime objects directly via client library
            if isinstance(ts, datetime):
                return ts.strftime("%Y-%m-%d %H:%M"), ts.isoformat()
            # Fallback: best-effort string
            try:
                parsed = datetime.fromisoformat(str(ts))
                return parsed.strftime("%Y-%m-%d %H:%M"), parsed.isoformat()
            except Exception:
                return str(ts), None

        for m in filtered_members:
            doc = reminder_lookup.get(m.get("discordID"))
            if doc:
                human, iso = format_ts(doc.get("lastReminderAt"))
                m["last_reminder_at"] = human
                m["last_reminder_iso"] = iso
                m["reminder_count"] = doc.get("reminderCount", 0)
            else:
                m["last_reminder_at"] = None
                m["last_reminder_iso"] = None
                m["reminder_count"] = 0

        if is_html_request:
            return render_template(
                "member_outreach.html",
                members=filtered_members,
                total=len(filtered_members),
            )

        # Fallback JSON (not the primary use case)
        return jsonify(
            {
                "members": filtered_members,
                "count": len(filtered_members),
            }
        )
    except Exception as e:
        if request.headers.get('Accept', '').find('text/html') >= 0:
            return f"<h1>Error</h1><p>{str(e)}</p>", 500
        return jsonify({"error": str(e)}), 500

@app.route('/api/assign_zwift_id', methods=['POST'])
def assign_zwift_id():
    """Assign a ZwiftID to a Discord user"""
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400
            
        discord_id = data.get('discord_id')
        zwift_id = data.get('zwift_id')
        username = data.get('username')
        
        if not discord_id:
            return jsonify({"status": "error", "message": "No Discord ID provided"}), 400
            
        if not zwift_id:
            return jsonify({"status": "error", "message": "No Zwift ID provided"}), 400
        
        # Update the Discord user with the ZwiftID
        result = firebase.update_discord_zwift_link(discord_id, zwift_id, username)
        
        return jsonify({
            "status": "success", 
            "discord_id": discord_id, 
            "zwift_id": zwift_id,
            "username": username,
            "operation": result.get("status", "updated")
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/discord/reconcile-verified-role', methods=['POST'])
@login_required
def reconcile_verified_member_role():
    """
    Reconcile the Verified Member role on Discord against current eligibility rules.

    Current baseline eligibility: user has linked Zwift ID (users.discordId -> users.zwiftId).
    Optional additional requirements (future-proofing):
      - requireCompanion: Zwift ID exists in companion_club_members/{zwiftId}
      - requireZwiftPower: Zwift ID exists in zwiftpower_club_members/{zwiftId}

    Body (optional JSON):
      {
        "requireZwiftId": true,
        "requireCompanion": false,
        "requireZwiftPower": false,
        "dryRun": false
      }
    """
    try:
        data = request.get_json(silent=True) or {}
        require_zwift = bool(data.get("requireZwiftId", True))
        require_companion = bool(data.get("requireCompanion", False))
        require_zwiftpower = bool(data.get("requireZwiftPower", False))
        dry_run = bool(data.get("dryRun", False))

        role_id = str(VERIFIED_MEMBER_ROLE_ID or "").strip()
        if not role_id:
            return jsonify({"error": "VERIFIED_MEMBER_ROLE_ID not configured"}), 400

        guild_id = os.environ.get("DISCORD_GUILD_ID")
        bot_token = os.environ.get("DISCORD_BOT_TOKEN")
        if not guild_id or not bot_token:
            return jsonify({"error": "Discord env not configured (DISCORD_GUILD_ID/DISCORD_BOT_TOKEN)"}), 500

        # Build discordId -> zwiftId lookup from Firebase users
        discord_to_zwift = {}
        try:
            users = firebase.get_collection("users", limit=100000) or []
            for u in users:
                try:
                    did = u.get("discordId")
                    zid = u.get("zwiftId")
                    if did is None:
                        continue
                    did_s = str(did).strip()
                    if not did_s:
                        continue
                    # Store zid even if None; we'll validate per requirements later
                    discord_to_zwift[did_s] = zid
                except Exception:
                    continue
        except Exception as e:
            return jsonify({"error": f"Failed to load users from Firebase: {str(e)}"}), 500

        # Optional roster sets (loaded only if requested)
        companion_ids = set()
        zwiftpower_ids = set()
        if require_companion:
            try:
                companion_ids = set([doc.id for doc in firebase.db.collection("companion_club_members").stream()])
            except Exception as comp_err:
                return jsonify({"error": f"Failed to load companion roster: {str(comp_err)}"}), 500
        if require_zwiftpower:
            try:
                zwiftpower_ids = set([doc.id for doc in firebase.db.collection("zwiftpower_club_members").stream()])
            except Exception as zp_err:
                return jsonify({"error": f"Failed to load ZwiftPower roster: {str(zp_err)}"}), 500

        # Fetch all Discord members (use high limit to avoid truncation)
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        members = discord_api.get_all_members(limit=100000, include_role_names=False) or []

        headers = {"Authorization": f"Bot {bot_token}"}

        result = {
            "dry_run": dry_run,
            "role_id": role_id,
            "require_zwift_id": require_zwift,
            "require_companion": require_companion,
            "require_zwiftpower": require_zwiftpower,
            "total_members": len(members),
            "eligible": 0,
            "ineligible": 0,
            "added": 0,
            "removed": 0,
            "unchanged": 0,
            "errors": 0,
        }

        for m in members:
            discord_id = str(m.get("discordID") or "").strip()
            if not discord_id:
                continue
            role_ids = [str(r) for r in (m.get("role_ids") or [])]
            has_verified = role_id in role_ids

            zwift_id = discord_to_zwift.get(discord_id)
            has_zwift = zwift_id is not None and str(zwift_id).strip() != ""
            zwift_id_str = str(zwift_id).strip() if has_zwift else ""

            eligible = True
            if require_zwift and not has_zwift:
                eligible = False
            if eligible and require_companion and zwift_id_str not in companion_ids:
                eligible = False
            if eligible and require_zwiftpower and zwift_id_str not in zwiftpower_ids:
                eligible = False

            if eligible:
                result["eligible"] += 1
            else:
                result["ineligible"] += 1

            # Only call Discord when a change is needed
            if eligible and not has_verified:
                if dry_run:
                    result["added"] += 1
                else:
                    try:
                        r = requests.put(
                            f"https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}/roles/{role_id}",
                            headers=headers,
                        )
                        if 200 <= r.status_code < 300:
                            result["added"] += 1
                        else:
                            # 403/404 treated as non-fatal (missing perms / user not found)
                            if r.status_code not in (403, 404):
                                result["errors"] += 1
                    except Exception:
                        result["errors"] += 1
            elif (not eligible) and has_verified:
                if dry_run:
                    result["removed"] += 1
                else:
                    try:
                        r = requests.delete(
                            f"https://discord.com/api/v10/guilds/{guild_id}/members/{discord_id}/roles/{role_id}",
                            headers=headers,
                        )
                        # 204 expected; 404 ok (already absent)
                        if r.status_code in (200, 202, 204, 404):
                            result["removed"] += 1
                        else:
                            result["errors"] += 1
                    except Exception:
                        result["errors"] += 1
            else:
                result["unchanged"] += 1

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/member_outreach/send', methods=['POST'])
@login_required
def send_member_outreach():
    """
    Send DM messages to selected members and record reminder/ outreach metadata.

    Expected JSON body:
    {
      "members": [{ "discord_id": "...", "username": "..." }, ...],
      "messageTemplate": "Hej {{username}} ..."
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        members = data.get("members") or []
        message_template = data.get("messageTemplate") or ""

        if not members:
            return jsonify({"status": "error", "message": "No members provided"}), 400

        if not message_template.strip():
            return jsonify({"status": "error", "message": "Message template is empty"}), 400

        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)

        sent = 0
        skipped = 0
        updated_entries = []

        from datetime import datetime

        # Preload existing reminder docs to avoid per-user queries
        existing_docs = firebase.get_collection(
            "discord_zwift_reminders", limit=10000, include_id=True
        )
        existing_lookup = {doc.get("id"): doc for doc in existing_docs}

        for item in members:
            discord_id = (item or {}).get("discord_id")
            username = (item or {}).get("username") or ""
            if not discord_id:
                skipped += 1
                continue

            # Personalize message
            msg = message_template.replace("{{username}}", username)

            ok = discord_api.send_direct_message(discord_id, msg)
            if not ok:
                skipped += 1
                continue

            sent += 1

            # Update Firestore reminder doc
            existing = existing_lookup.get(discord_id) or {}
            new_count = int(existing.get("reminderCount", 0) or 0) + 1
            doc_data = {
                "discordID": discord_id,
                "lastReminderAt": datetime.utcnow(),
                "reminderCount": new_count,
                "lastReminderMessage": msg,
            }
            firebase.set_document(
                "discord_zwift_reminders", discord_id, doc_data, merge=False
            )
            existing_lookup[discord_id] = doc_data

            updated_entries.append(
                {"discord_id": discord_id, "reminder_count": new_count}
            )

        return jsonify(
            {
                "status": "success",
                "sent": sent,
                "skipped": skipped,
                "updated": updated_entries,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/initialize_rider_queue', methods=['POST'])
def initialize_rider_queue():
    """Initialize a queue of riders that need racing scores"""
    try:
        # Get the latest club_stats
        club_stats = firebase.get_latest_document("club_stats")
        
        if not club_stats or len(club_stats) == 0:
            return jsonify({"status": "error", "message": "No club_stats data found"}), 404
            
        # Get list of riders
        stats = club_stats[0]
        riders = stats["data"]["riders"]
        
        # Create or update the single queue document
        queue_doc_ref = firebase.db.collection("rider_queues").document("current")
        
        # Create lists for each status
        pending_riders = []
        
        # Add riders without racing scores to pending list
        for rider in riders:
            if "riderId" not in rider:
                continue
                
            # Only queue riders without racing scores
            if "racingScore" not in rider:
                rider_id = rider["riderId"]
                rider_name = rider.get("name", "Unknown")
                
                # Add to pending list
                pending_riders.append({
                    "riderId": rider_id,
                    "name": rider_name,
                    "addedAt": datetime.now()
                })
        
        # Create the queue document
        queue_doc_ref.set({
            "created": datetime.now(),
            "pendingRiders": pending_riders,
            "completedRiders": [],
            "failedRiders": [],
            "stats": {
                "total": len(pending_riders),
                "pending": len(pending_riders),
                "completed": 0,
                "failed": 0
            }
        })
        
        return jsonify({
            "status": "success",
            "message": f"Initialized queue with {len(pending_riders)} riders",
            "total_riders": len(riders),
            "queued_riders": len(pending_riders)
        })
        
    except Exception as e:
        print(f"Error initializing rider queue: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/process_rider_queue', methods=['POST'])
def process_rider_queue():
    """Process a batch of riders from the rider queue"""
    try:
        # Get parameters
        batch_size = request.json.get('batch_size', 3) if request.json else 3
        
        # Get the queue document
        queue_doc_ref = firebase.db.collection("rider_queues").document("current")
        queue_doc = queue_doc_ref.get()
        
        if not queue_doc.exists:
            return jsonify({
                "status": "success",
                "message": "No queue exists. Call initialize_rider_queue first.",
                "queue_empty": True,
                "queue_exists": False
            })
        
        queue_data = queue_doc.to_dict()
        pending_riders = queue_data.get("pendingRiders", [])
        completed_riders = queue_data.get("completedRiders", [])
        failed_riders = queue_data.get("failedRiders", [])
        
        # Check if there are any pending riders
        if not pending_riders:
            # No more pending riders, check if we should update club_stats
            if completed_riders:
                # Update the main club_stats with all processed scores
                return update_club_stats_from_queue()
            else:
                return jsonify({
                    "status": "success",
                    "message": "No riders in queue to process"
                })
        
        # Determine batch to process (up to batch_size riders)
        batch_to_process = pending_riders[:batch_size]
        remaining_pending = pending_riders[batch_size:]
        
        # Get cached ZwiftAPI instance
        zwift_api = get_authenticated_zwift_api()
        
        processed_count = 0
        success_count = 0
        
        # Process each rider
        for rider in batch_to_process:
            rider_id = rider.get("riderId")
            rider_name = rider.get("name", "Unknown")
            
            if not rider_id:
                continue
                
            processed_count += 1
            print(f"Processing rider {rider_name} (ID: {rider_id})")
            
            try:
                # Get the rider's profile
                profile = zwift_api.get_profile(rider_id)
                
                # Add racing score if available
                if profile and "competitionMetrics" in profile and "racingScore" in profile["competitionMetrics"]:
                    racing_score = profile["competitionMetrics"]["racingScore"]
                    
                    # Add to completed list
                    rider["processedAt"] = datetime.now()
                    rider["racingScore"] = racing_score
                    completed_riders.append(rider)
                    
                    success_count += 1
                    print(f"Added racing score {racing_score} to rider {rider_name}")
                else:
                    # Add to failed list
                    rider["processedAt"] = datetime.now()
                    rider["error"] = "Racing score not found in profile"
                    failed_riders.append(rider)
                    
                    print(f"Could not find racing score for rider {rider_name}")
            except Exception as rider_error:
                # Add to failed list with error
                rider["processedAt"] = datetime.now()
                rider["error"] = str(rider_error)
                failed_riders.append(rider)
                
                print(f"Error processing rider {rider_name}: {str(rider_error)}")
            
            # Add delay between riders
            time.sleep(5)
        
        # Update the queue document with new lists
        queue_doc_ref.update({
            "pendingRiders": remaining_pending,
            "completedRiders": completed_riders,
            "failedRiders": failed_riders,
            "stats": {
                "total": len(remaining_pending) + len(completed_riders) + len(failed_riders),
                "pending": len(remaining_pending),
                "completed": len(completed_riders),
                "failed": len(failed_riders)
            }
        })
        
        return jsonify({
            "status": "success",
            "message": f"Processed {processed_count} riders, {success_count} successful",
            "stats": {
                "processed_this_batch": processed_count,
                "successful_this_batch": success_count,
                "pending": len(remaining_pending),
                "completed": len(completed_riders),
                "failed": len(failed_riders)
            },
            "queue_empty": len(remaining_pending) == 0
        })
        
    except Exception as e:
        print(f"Error processing rider queue: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

def update_club_stats_from_queue():
    """Update club_stats with all processed racing scores from the queue"""
    try:
        # Get the latest club_stats
        club_stats_docs = firebase.get_latest_document("club_stats")
        
        if not club_stats_docs or len(club_stats_docs) == 0:
            return jsonify({"status": "error", "message": "No club_stats data found"}), 404
        
        # Get the queue document
        queue_doc_ref = firebase.db.collection("rider_queues").document("current")
        queue_doc = queue_doc_ref.get()
        
        if not queue_doc.exists:
            return jsonify({
                "status": "error",
                "message": "Queue not found"
            }), 404
            
        queue_data = queue_doc.to_dict()
        completed_riders = queue_data.get("completedRiders", [])
        
        if not completed_riders:
            return jsonify({
                "status": "success",
                "message": "No completed riders in queue to update club_stats with"
            })
        
        # Create a mapping of rider IDs to racing scores
        completed_dict = {str(rider["riderId"]): rider["racingScore"] for rider in completed_riders}
        
        # Get the original document
        stats = club_stats_docs[0]
        
        # Get the document ID of the latest club_stats
        # This assumes get_latest_document returns documents ordered by timestamp
        # and includes document IDs in the result
        latest_doc_ref = None
        latest_stats = None
        
        # Get the actual document reference to update
        club_stats_query = firebase.db.collection("club_stats").order_by("timestamp", direction=firebase.firestore.Query.DESCENDING).limit(1)
        latest_docs = list(club_stats_query.stream())
        if latest_docs:
            latest_doc_ref = latest_docs[0].reference
            latest_stats = latest_docs[0].to_dict()
        else:
            return jsonify({"status": "error", "message": "Could not find club_stats document to update"}), 500
        
        # Update the stats object with racing scores
        updated_count = 0
        
        for rider in stats["data"]["riders"]:
            if "riderId" not in rider:
                continue
                
            rider_id_str = str(rider["riderId"])
            if rider_id_str in completed_dict:
                rider["racingScore"] = completed_dict[rider_id_str]
                updated_count += 1
        
        # Update the existing document with the new data
        update_data = {
            "data": stats["data"]
            # Keep other fields as they are
        }
        
        # Update the document
        latest_doc_ref.update(update_data)
        
        # Clear the queue if requested
        should_clear_queue = request.json.get('clear_queue', True) if request.json else True
        if should_clear_queue:
            queue_doc_ref.delete()
        
        return jsonify({
            "status": "success",
            "message": f"Updated club_stats with {updated_count} racing scores from queue",
            "queue_cleared": should_clear_queue
        })
        
    except Exception as e:
        print(f"Error updating club_stats from queue: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/content/messages', methods=['GET'])
@login_required
def content_messages():
    """Web interface for managing Discord bot messages"""
    try:
        # Check if the request accepts HTML
        is_html_request = request.headers.get('Accept', '').find('text/html') >= 0
        
        if is_html_request:
            # For HTML requests, render the management interface
            return render_template('content_messages.html', api_key=CONTENT_API_KEY)
        else:
            # For API requests, return summary data
            welcome_messages = firebase.get_collection('welcome_messages', limit=100, include_id=True)
            role_messages = firebase.get_collection('role_messages', limit=100, include_id=True)
            scheduled_messages = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
            
            return jsonify({
                "welcome_messages": welcome_messages,
                "role_messages": role_messages,
                "scheduled_messages": scheduled_messages,
                "stats": {
                    "total_welcome": len(welcome_messages),
                    "total_scheduled": len(scheduled_messages) + len(role_messages),
                    "active_welcome": len([m for m in welcome_messages if m.get('active', False)]),
                    "active_scheduled": len([m for m in scheduled_messages if m.get('active', False)]) + len([m for m in role_messages if m.get('active', False)])
                }
            })
    except Exception as e:
        if is_html_request:
            return f"<h1>Error</h1><p>{str(e)}</p>", 500
        return jsonify({"error": str(e)}), 500

@app.route('/api/messages/welcome-messages', methods=['GET'])
def get_welcome_messages():
    """Get all active welcome messages for the Discord bot"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Get welcome messages from Firebase with document IDs
        messages = firebase.get_collection('welcome_messages', limit=100, include_id=True)
        
        # Filter only active messages
        active_messages = [msg for msg in messages if msg.get('active', False)]
        
        return jsonify(active_messages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/messages/welcome-messages', methods=['POST'])
def create_welcome_message():
    """Create a new welcome message"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Add metadata
        message_data = {
            **data,
            "created_at": datetime.now(),
            "created_by": "admin",
            "active": data.get("active", True)
        }
        
        # Save to Firebase
        doc_ref = firebase.db.collection('welcome_messages').add(message_data)
        message_data["id"] = doc_ref[1].id
        
        return jsonify({"status": "success", "message": message_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedules/due', methods=['GET'])
def get_due_scheduled_messages():
    """Get scheduled messages that are due to be sent"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Get all active schedules with document IDs
        schedules = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
        
        due_messages = []
        # Get current time as datetime
        from datetime import datetime, timezone
        import pytz
        
        # Use Central European Time for consistency
        cet = pytz.timezone('Europe/Berlin')
        current_time = datetime.now(cet)
        
        print(f"[DEBUG] Checking for due messages at {current_time}")
        
        for schedule in schedules:
            if not schedule.get('active', False):
                continue
                
            # Check if message is due
            next_run = schedule.get('next_run')
            last_sent = schedule.get('last_sent')
            
            print(f"[DEBUG] Schedule {schedule.get('id', 'unknown')}: next_run={next_run}, last_sent={last_sent}")
            
            if next_run:
                # Convert Firebase timestamp to datetime for comparison
                if hasattr(next_run, 'seconds'):
                    # It's a Firebase Timestamp - convert to CET
                    next_run_datetime = datetime.fromtimestamp(next_run.seconds, tz=cet)
                elif isinstance(next_run, datetime):
                    # Already a datetime - ensure it's in CET
                    if next_run.tzinfo is None:
                        next_run_datetime = cet.localize(next_run)
                    else:
                        next_run_datetime = next_run.astimezone(cet)
                else:
                    # Try converting to datetime and set to CET
                    try:
                        next_run_datetime = datetime.fromisoformat(str(next_run))
                        if next_run_datetime.tzinfo is None:
                            next_run_datetime = cet.localize(next_run_datetime)
                        else:
                            next_run_datetime = next_run_datetime.astimezone(cet)
                    except:
                        continue
                
                print(f"[DEBUG] Schedule {schedule.get('id', 'unknown')}: next_run_datetime={next_run_datetime}, current_time={current_time}")
                
                # Check if it's due
                if current_time >= next_run_datetime:
                    print(f"[DEBUG] Schedule {schedule.get('id', 'unknown')} is DUE!")
                    due_messages.append(schedule)
                else:
                    print(f"[DEBUG] Schedule {schedule.get('id', 'unknown')} is not due yet")
        
        print(f"[DEBUG] Found {len(due_messages)} due messages")
        return jsonify(due_messages)
    except Exception as e:
        print(f"Error in get_due_scheduled_messages: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedules/<schedule_id>/sent', methods=['POST'])
def mark_scheduled_message_sent(schedule_id):
    """Mark a scheduled message as sent and calculate next run time"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        print(f"[DEBUG] Marking schedule {schedule_id} as sent")
        
        # Get the schedule document
        doc_ref = firebase.db.collection('scheduled_messages').document(schedule_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            print(f"[DEBUG] Schedule {schedule_id} not found")
            return jsonify({"error": "Schedule not found"}), 404
        
        schedule_data = doc.to_dict()
        schedule_config = schedule_data.get('schedule', {})
        
        print(f"[DEBUG] Schedule {schedule_id} config: {schedule_config}")
        
        # Calculate next run time based on schedule type
        from datetime import datetime, timezone, timedelta
        import calendar
        import pytz
        
        # Use Central European Time
        cet = pytz.timezone('Europe/Berlin')  # CET/CEST timezone
        current_time = datetime.now(cet)
        next_run = None
        
        schedule_type = schedule_config.get('type', 'weekly')
        schedule_time = schedule_config.get('time', '18:00')
        
        print(f"[DEBUG] Schedule {schedule_id}: type={schedule_type}, time={schedule_time}")
        
        # Parse the schedule time (format: "HH:MM")
        try:
            time_parts = schedule_time.split(':')
            schedule_hour = int(time_parts[0])
            schedule_minute = int(time_parts[1])
        except (ValueError, IndexError):
            schedule_hour = 18
            schedule_minute = 0
        
        if schedule_type == 'daily':
            # Calculate next daily occurrence (tomorrow at the scheduled time)
            next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0) + timedelta(days=1)
            
        elif schedule_type == 'weekly':
            # Calculate next weekly occurrence (same day next week)
            next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0) + timedelta(weeks=1)
            
        elif schedule_type == 'monthly':
            # Get the target day of month (default to current day if not specified)
            target_day = schedule_config.get('day_of_month', current_time.day)
            
            # Calculate next monthly occurrence
            next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
            
            # Handle month rollover
            if current_time.month == 12:
                next_run = next_run.replace(year=current_time.year + 1, month=1)
            else:
                next_run = next_run.replace(month=current_time.month + 1)
                
            # Handle day overflow (e.g., Jan 31 -> Feb 28)
            try:
                next_run = next_run.replace(day=target_day)
            except ValueError:
                # Day doesn't exist in target month, use last day of month
                last_day = calendar.monthrange(next_run.year, next_run.month)[1]
                next_run = next_run.replace(day=last_day)
        
        print(f"[DEBUG] Schedule {schedule_id}: calculated next_run={next_run}")
        
        # Update the document
        update_data = {
            'last_sent': current_time,
            'next_run': next_run
        }
        
        print(f"[DEBUG] Schedule {schedule_id}: updating with last_sent={current_time}, next_run={next_run}")
        
        doc_ref.update(update_data)
        
        print(f"[DEBUG] Schedule {schedule_id}: successfully updated")
        
        return jsonify({
            "status": "success", 
            "next_run": next_run.isoformat() if next_run else None
        })
    except Exception as e:
        print(f"Error in mark_scheduled_message_sent: {str(e)}")  # For debugging
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedules', methods=['GET', 'POST'])
def manage_scheduled_messages():
    """Get all scheduled messages or create a new one"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        if request.method == 'GET':
            schedules = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
            return jsonify(schedules)
            
        elif request.method == 'POST':
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400
            
            # Calculate initial next_run time
            schedule_config = data.get('schedule', {})
            next_run = None
            
            if schedule_config:
                from datetime import datetime, timezone, timedelta
                import calendar
                
                # Use Central European Time
                cet = pytz.timezone('Europe/Berlin')  # CET/CEST timezone
                current_time = datetime.now(cet)
                
                schedule_type = schedule_config.get('type', 'weekly')
                
                # For probability-based scheduling, don't calculate next_run
                if schedule_type == 'probability':
                    next_run = None  # Probability-based messages don't use next_run
                else:
                    schedule_time = schedule_config.get('time', '18:00')  # Default to 6 PM
                    
                    # Parse the schedule time (format: "HH:MM")
                    try:
                        time_parts = schedule_time.split(':')
                        schedule_hour = int(time_parts[0])
                        schedule_minute = int(time_parts[1])
                    except (ValueError, IndexError):
                        schedule_hour = 18
                        schedule_minute = 0
                    
                    if schedule_type == 'daily':
                        # Calculate next daily occurrence
                        next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                        # If the time has already passed today, schedule for tomorrow
                        if next_run <= current_time:
                            next_run += timedelta(days=1)
                            
                    elif schedule_type == 'weekly':
                        schedule_day = schedule_config.get('day', 'monday').lower()
                        
                        # Map day names to weekday numbers (Monday = 0)
                        day_map = {
                            'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                            'friday': 4, 'saturday': 5, 'sunday': 6
                        }
                        target_weekday = day_map.get(schedule_day, 0)
                        
                        # Calculate next weekly occurrence
                        current_weekday = current_time.weekday()
                        days_ahead = target_weekday - current_weekday
                        
                        if days_ahead < 0:  # Target day already happened this week
                            days_ahead += 7
                        elif days_ahead == 0:  # Target day is today
                            # Check if the time has already passed
                            today_at_time = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                            if today_at_time <= current_time:
                                days_ahead = 7  # Schedule for next week
                        
                        next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0) + timedelta(days=days_ahead)
                        
                    elif schedule_type == 'monthly':
                        # Get the target day of month (default to current day if not specified)
                        target_day = schedule_config.get('day_of_month', current_time.day)
                        
                        # Calculate next monthly occurrence
                        next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                        
                        # Try to set the target day for this month first
                        try:
                            next_run = next_run.replace(day=target_day)
                        except ValueError:
                            # Day doesn't exist in current month, use last day of month
                            last_day = calendar.monthrange(next_run.year, next_run.month)[1]
                            next_run = next_run.replace(day=last_day)
                        
                        # If the time has already passed this month, schedule for next month
                        if next_run <= current_time:
                            # Handle month rollover
                            if current_time.month == 12:
                                next_run = next_run.replace(year=current_time.year + 1, month=1)
                            else:
                                next_run = next_run.replace(month=current_time.month + 1)
                                
                            # Handle day overflow for next month (e.g., Jan 31 -> Feb 28)
                            try:
                                next_run = next_run.replace(day=target_day)
                            except ValueError:
                                # Day doesn't exist in target month, use last day of month
                                last_day = calendar.monthrange(next_run.year, next_run.month)[1]
                                next_run = next_run.replace(day=last_day)
            
            # Add metadata - use datetime objects directly
            schedule_data = {
                **data,
                "created_at": datetime.now(timezone.utc),
                "created_by": "admin",
                "active": data.get("active", True),
                "next_run": next_run,  # Firebase will handle the conversion
                "last_sent": None
            }
            
            # Save to Firebase
            doc_ref = firebase.db.collection('scheduled_messages').add(schedule_data)
            schedule_data["id"] = doc_ref[1].id
            
            return jsonify({"status": "success", "schedule": schedule_data})
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/api/messages/welcome-messages/<message_id>', methods=['DELETE'])
def delete_welcome_message(message_id):
    """Delete a welcome message"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Delete the document from Firebase
        doc_ref = firebase.db.collection('welcome_messages').document(message_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({"error": "Message not found"}), 404
        
        doc_ref.delete()
        
        return jsonify({"status": "success", "message": "Welcome message deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/messages/welcome-messages/<message_id>', methods=['PUT'])
def update_welcome_message(message_id):
    """Update a welcome message"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Get the document reference
        doc_ref = firebase.db.collection('welcome_messages').document(message_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({"error": "Message not found"}), 404
        
        # Add update metadata
        from datetime import datetime, timezone
        update_data = {
            **data,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": "admin"
        }
        
        # Handle embed removal - if embed is explicitly set to null, delete the field
        if 'embed' in update_data and update_data['embed'] is None:
            from google.cloud.firestore_v1 import DELETE_FIELD
            update_data['embed'] = DELETE_FIELD
        
        # Update the document
        doc_ref.update(update_data)
        
        # Return updated document
        updated_doc = doc_ref.get().to_dict()
        updated_doc["id"] = message_id
        
        return jsonify({"status": "success", "message": updated_doc})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/messages/role-messages', methods=['GET'])
def get_role_messages():
    """Get all role messages for the Discord bot"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Get role messages from Firebase with document IDs
        messages = firebase.get_collection('role_messages', limit=100, include_id=True)
        return jsonify(messages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/messages/role-messages', methods=['POST'])
def create_role_message():
    """Create a new role message"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Validate required fields
        if not data.get('title') or not data.get('content') or not data.get('role_id') or not data.get('channel_id'):
            return jsonify({"error": "Missing required fields: title, content, role_id, channel_id"}), 400
        
        # Create message data
        from datetime import datetime, timezone
        message_data = {
            'title': data['title'],
            'content': data['content'],
            'role_id': data['role_id'],
            'channel_id': data['channel_id'],
            'active': data.get('active', True),
            'created_at': datetime.now(timezone.utc),
            'created_by': 'admin'
        }
        
        # Add embed if provided
        if 'embed' in data and data['embed'] is not None:
            message_data['embed'] = data['embed']
        
        # Add to Firebase
        doc_ref = firebase.db.collection('role_messages').add(message_data)
        
        return jsonify({"status": "success", "message": "Role message created", "id": doc_ref[1].id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/messages/role-messages/<message_id>', methods=['DELETE'])
def delete_role_message(message_id):
    """Delete a role message"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Delete the document from Firebase
        doc_ref = firebase.db.collection('role_messages').document(message_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({"error": "Message not found"}), 404
        
        doc_ref.delete()
        
        return jsonify({"status": "success", "message": "Role message deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/messages/role-messages/<message_id>', methods=['PUT'])
def update_role_message(message_id):
    """Update a role message"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Get the document reference
        doc_ref = firebase.db.collection('role_messages').document(message_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({"error": "Message not found"}), 404
        
        # Add update metadata
        from datetime import datetime, timezone
        update_data = {
            **data,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": "admin"
        }
        
        # Handle embed removal - if embed is explicitly set to null, delete the field
        if 'embed' in update_data and update_data['embed'] is None:
            from google.cloud.firestore_v1 import DELETE_FIELD
            update_data['embed'] = DELETE_FIELD
        
        # Update the document
        doc_ref.update(update_data)
        
        # Return updated document
        updated_doc = doc_ref.get().to_dict()
        updated_doc["id"] = message_id
        
        return jsonify({"status": "success", "message": updated_doc})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedules/<schedule_id>', methods=['DELETE'])
def delete_scheduled_message(schedule_id):
    """Delete a scheduled message"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Delete the document from Firebase
        doc_ref = firebase.db.collection('scheduled_messages').document(schedule_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({"error": "Schedule not found"}), 404
        
        doc_ref.delete()
        
        return jsonify({"status": "success", "message": "Scheduled message deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedules/<schedule_id>', methods=['PUT'])
def update_scheduled_message(schedule_id):
    """Update a scheduled message"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Get the document reference
        doc_ref = firebase.db.collection('scheduled_messages').document(schedule_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({"error": "Schedule not found"}), 404
        
        # Recalculate next_run time if schedule changed
        from datetime import datetime, timezone, timedelta
        import calendar
        import pytz
        
        schedule_config = data.get('schedule', {})
        if schedule_config:
            # Use Central European Time
            cet = pytz.timezone('Europe/Berlin')
            current_time = datetime.now(cet)
            
            schedule_type = schedule_config.get('type', 'weekly')
            
            # For probability-based scheduling, don't calculate next_run
            if schedule_type == 'probability':
                data['next_run'] = None  # Probability-based messages don't use next_run
            else:
                schedule_time = schedule_config.get('time', '18:00')
                
                # Parse the schedule time (format: "HH:MM")
                try:
                    time_parts = schedule_time.split(':')
                    schedule_hour = int(time_parts[0])
                    schedule_minute = int(time_parts[1])
                except (ValueError, IndexError):
                    schedule_hour = 18
                    schedule_minute = 0
                
                if schedule_type == 'daily':
                    # Calculate next daily occurrence
                    next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                    # If the time has already passed today, schedule for tomorrow
                    if next_run <= current_time:
                        next_run += timedelta(days=1)
                    data['next_run'] = next_run
                        
                elif schedule_type == 'weekly':
                    schedule_day = schedule_config.get('day', 'monday').lower()
                    
                    # Map day names to weekday numbers (Monday = 0)
                    day_map = {
                        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                        'friday': 4, 'saturday': 5, 'sunday': 6
                    }
                    target_weekday = day_map.get(schedule_day, 0)
                    
                    # Calculate next weekly occurrence
                    current_weekday = current_time.weekday()
                    days_ahead = target_weekday - current_weekday
                    
                    if days_ahead < 0:  # Target day already happened this week
                        days_ahead += 7
                    elif days_ahead == 0:  # Target day is today
                        # Check if the time has already passed
                        today_at_time = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                        if today_at_time <= current_time:
                            days_ahead = 7  # Schedule for next week
                    
                    next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0) + timedelta(days=days_ahead)
                    data['next_run'] = next_run
                    
                elif schedule_type == 'monthly':
                    # Get the target day of month (default to current day if not specified)
                    target_day = schedule_config.get('day_of_month', current_time.day)
                    
                    # Calculate next monthly occurrence
                    next_run = current_time.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                    
                    # Try to set the target day for this month first
                    try:
                        next_run = next_run.replace(day=target_day)
                    except ValueError:
                        # Day doesn't exist in current month, use last day of month
                        last_day = calendar.monthrange(next_run.year, next_run.month)[1]
                        next_run = next_run.replace(day=last_day)
                    
                    # If the time has already passed this month, schedule for next month
                    if next_run <= current_time:
                        # Handle month rollover
                        if current_time.month == 12:
                            next_run = next_run.replace(year=current_time.year + 1, month=1)
                        else:
                            next_run = next_run.replace(month=current_time.month + 1)
                            
                        # Handle day overflow for next month (e.g., Jan 31 -> Feb 28)
                        try:
                            next_run = next_run.replace(day=target_day)
                        except ValueError:
                            # Day doesn't exist in target month, use last day of month
                            last_day = calendar.monthrange(next_run.year, next_run.month)[1]
                            next_run = next_run.replace(day=last_day)
                    
                    data['next_run'] = next_run
        
        # Add update metadata
        update_data = {
            **data,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": "admin"
        }
        
        # Update the document
        doc_ref.update(update_data)
        
        # Return updated document
        updated_doc = doc_ref.get().to_dict()
        updated_doc["id"] = schedule_id
        
        return jsonify({"status": "success", "schedule": updated_doc})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedules/debug', methods=['GET'])
def debug_scheduled_messages():
    """Debug endpoint to check the current status of all scheduled messages"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Get all schedules with document IDs
        schedules = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
        
        from datetime import datetime
        import pytz
        
        # Use Central European Time for consistency
        cet = pytz.timezone('Europe/Berlin')
        current_time = datetime.now(cet)
        
        debug_info = {
            "current_time": current_time.isoformat(),
            "total_schedules": len(schedules),
            "schedules": []
        }
        
        for schedule in schedules:
            next_run = schedule.get('next_run')
            last_sent = schedule.get('last_sent')
            
            # Convert timestamps to readable format
            next_run_str = None
            last_sent_str = None
            
            if next_run:
                if hasattr(next_run, 'seconds'):
                    next_run_datetime = datetime.fromtimestamp(next_run.seconds, tz=cet)
                    next_run_str = next_run_datetime.isoformat()
                elif isinstance(next_run, datetime):
                    if next_run.tzinfo is None:
                        next_run_datetime = cet.localize(next_run)
                    else:
                        next_run_datetime = next_run.astimezone(cet)
                    next_run_str = next_run_datetime.isoformat()
            
            if last_sent:
                if hasattr(last_sent, 'seconds'):
                    last_sent_datetime = datetime.fromtimestamp(last_sent.seconds, tz=cet)
                    last_sent_str = last_sent_datetime.isoformat()
                elif isinstance(last_sent, datetime):
                    if last_sent.tzinfo is None:
                        last_sent_datetime = cet.localize(last_sent)
                    else:
                        last_sent_datetime = last_sent.astimezone(cet)
                    last_sent_str = last_sent_datetime.isoformat()
            
            schedule_debug = {
                "id": schedule.get('id'),
                "title": schedule.get('title', 'No title'),
                "active": schedule.get('active', False),
                "schedule_type": schedule.get('schedule', {}).get('type'),
                "schedule_time": schedule.get('schedule', {}).get('time'),
                "schedule_day": schedule.get('schedule', {}).get('day'),
                "next_run": next_run_str,
                "last_sent": last_sent_str,
                "is_due": next_run_str and current_time.isoformat() >= next_run_str if next_run_str else False
            }
            
            debug_info["schedules"].append(schedule_debug)
        
        return jsonify(debug_info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -------------------- Membership Admin (Settings + Payments) --------------------
@app.route('/membership', methods=['GET'])
@login_required
def membership_admin():
    """
    Admin UI for managing Club Membership settings and viewing payments.
    """
    try:
        return render_template('membership_admin.html', user=session.get('user', {}))
    except Exception as e:
        flash(f'Error loading membership admin: {str(e)}', 'error')
        return render_template('membership_admin.html', user=session.get('user', {}))


@app.route('/api/membership/settings', methods=['GET'])
@login_required
def membership_settings_get():
    """
    Get membership settings stored under system_settings/global.membership
    """
    try:
        settings = firebase.get_document('system_settings', 'global') or {}
        membership = settings.get('membership', {}) if isinstance(settings, dict) else {}
        out = {
            "minAmountDkk": int(membership.get('minAmountDkk', 10)),
            "maxAmountDkk": int(membership.get('maxAmountDkk', 100)),
            "clubMemberRoleId": str(membership.get('clubMemberRoleId', '') or ''),
            "paymentOptions": membership.get('paymentOptions', []) if isinstance(membership.get('paymentOptions', []), list) else []
        }
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/membership/settings', methods=['POST'])
@login_required
def membership_settings_post():
    """
    Update membership settings under system_settings/global.membership
    """
    try:
        data = request.get_json(force=True) or {}
        min_amount = int(data.get('minAmountDkk', 10))
        max_amount = int(data.get('maxAmountDkk', 100))
        role_id = str(data.get('clubMemberRoleId') or '')
        payment_options = data.get('paymentOptions') or []
        if not isinstance(payment_options, list):
            payment_options = []

        if min_amount <= 0 or max_amount <= 0 or min_amount > max_amount:
            return jsonify({"error": "Invalid range: ensure min > 0, max > 0 and min <= max"}), 400

        settings = firebase.get_document('system_settings', 'global') or {}
        if not isinstance(settings, dict):
            settings = {}
        settings['membership'] = {
            "minAmountDkk": min_amount,
            "maxAmountDkk": max_amount,
            "clubMemberRoleId": role_id,
            "paymentOptions": payment_options,
            "updatedAt": datetime.now().isoformat()
        }
        firebase.set_document('system_settings', 'global', settings, merge=True)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/membership/payments', methods=['GET'])
@login_required
def membership_payments_list():
    """
    List recent membership payments from 'payments' collection.
    Optional query params:
      - limit: number of records (default 100)
      - status: filter by status (e.g., 'succeeded', 'created', 'failed')
    """
    try:
        limit = int(request.args.get('limit', 100))
        status_filter = request.args.get('status', '').strip().lower()
        docs = firebase.get_collection('payments', limit=limit, include_id=True) or []
        
        # Filter by status if provided
        if status_filter:
            docs = [p for p in docs if str(p.get('status') or '').lower() == status_filter]
        
        # Normalize provider fields for UI
        for p in docs:
            provider = str(p.get('paymentProvider') or '').strip().lower() or 'unknown'
            p['provider'] = provider

            # Handle vipps-checkout vs vipps (ePayment)
            vipps = p.get('vipps') or {}
            checkout = p.get('checkout') or {}
            
            if provider == 'vipps-checkout':
                p['providerState'] = str(checkout.get('state') or p.get('status') or '').upper()
                p['providerRef'] = str(checkout.get('reference') or p.get('id') or '')
            elif provider == 'vipps':
                vipps_state = str(vipps.get('state') or '').strip().upper()
                p['providerState'] = vipps_state or str(p.get('status') or '').upper()
                p['providerRef'] = str(vipps.get('reference') or p.get('id') or '')
            else:
                p['providerState'] = str(p.get('status') or '').upper()
                p['providerRef'] = str(p.get('id') or '')

        # Sort by createdAt desc (so we see newest payments first, including initiated ones)
        def parse_date(x):
            for field in ['createdAt', 'paidAt', 'updatedAt']:
                try:
                    val = x.get(field, '')
                    if val:
                        return datetime.fromisoformat(val.replace('Z', '+00:00'))
                except Exception:
                    pass
            return datetime.min
        docs.sort(key=parse_date, reverse=True)
        return jsonify({"payments": docs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/membership/payments.csv', methods=['GET'])
@login_required
def membership_payments_csv():
    """
    Download payments as CSV.
    """
    try:
        import io, csv
        # Large limit to include all
        payments = firebase.get_collection('payments', limit=100000, include_id=True) or []
        # Sort by createdAt desc
        def parse_date(x):
            for field in ['createdAt', 'paidAt', 'updatedAt']:
                try:
                    val = x.get(field, '')
                    if val:
                        return datetime.fromisoformat(val.replace('Z', '+00:00'))
                except Exception:
                    pass
            return datetime.min
        payments.sort(key=parse_date, reverse=True)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            'createdAt','paidAt','userId','fullName','userEmail','amountDkk','currency','status',
            'coveredThroughYear','coversYears','provider','providerState','providerRef','reference'
        ])
        for p in payments:
            vipps = p.get('vipps') or {}
            checkout = p.get('checkout') or {}
            provider = str(p.get('paymentProvider') or '').strip().lower() or 'unknown'
            provider_state = ''
            provider_ref = ''
            if provider == 'vipps-checkout':
                provider_state = str(checkout.get('state') or p.get('status') or '').upper()
                provider_ref = str(checkout.get('reference') or p.get('id') or '')
            elif provider == 'vipps':
                provider_state = str(vipps.get('state') or '').strip().upper()
                provider_ref = str(vipps.get('reference') or p.get('id') or '')
            else:
                provider_state = str(p.get('status') or '').upper()
                provider_ref = str(p.get('id') or '')
            writer.writerow([
                p.get('createdAt',''),
                p.get('paidAt',''),
                p.get('userId',''),
                p.get('fullName',''),
                p.get('userEmail',''),
                p.get('amountDkk',''),
                p.get('currency',''),
                p.get('status',''),
                p.get('coveredThroughYear',''),
                ','.join(map(str, p.get('coversYears') or [])),
                provider,
                provider_state,
                provider_ref,
                checkout.get('reference','') or vipps.get('reference',''),
            ])
        csv_data = buffer.getvalue()
        from flask import make_response
        resp = make_response(csv_data)
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = 'attachment; filename="payments.csv"'
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/membership/payments/totals-per-year', methods=['GET'])
@login_required
def membership_payments_totals_per_year():
    """
    Aggregate succeeded payments by calendar year (based on paidAt, fallback to createdAt)
    and return total amount (DKK) + count per year.
    """
    try:
        payments = firebase.get_collection('payments', limit=100000, include_id=True) or []

        totals = {}  # year -> {"totalAmountDkk": int, "count": int}

        def parse_dt(val: str):
            if not val:
                return None
            try:
                # Accept ISO strings with Z suffix
                return datetime.fromisoformat(str(val).replace('Z', '+00:00'))
            except Exception:
                return None

        for p in payments:
            try:
                if str(p.get('status', '')).lower() != 'succeeded':
                    continue

                dt = parse_dt(p.get('paidAt') or '') or parse_dt(p.get('createdAt') or '')
                if not dt:
                    continue

                year = int(dt.year)

                amt_raw = p.get('amountDkk', None)
                if amt_raw is None or amt_raw == '':
                    continue
                try:
                    amt = int(float(amt_raw))
                except Exception:
                    continue

                if year not in totals:
                    totals[year] = {"totalAmountDkk": 0, "count": 0}
                totals[year]["totalAmountDkk"] += amt
                totals[year]["count"] += 1
            except Exception:
                continue

        years = sorted(totals.keys(), reverse=True)
        out = [{"year": y, **totals[y]} for y in years]
        return jsonify({"totals": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route('/api/membership/reconcile-roles', methods=['POST'])
@login_required
def membership_reconcile_roles():
    """
    Recalculate membership coverage for all users based on successful payments and
    add/remove the configured Club Member role on Discord accordingly.
    Also updates memberships/{userId} with computed status and coverage.
    """
    try:
        # Load settings to get role id
        settings = firebase.get_document('system_settings', 'global') or {}
        membership = settings.get('membership', {}) if isinstance(settings, dict) else {}
        role_id = str(membership.get('clubMemberRoleId') or '').strip()
        if not role_id:
            return jsonify({"error": "Club Member Role ID not configured in settings"}), 400

        # Gather payments (large limit for safety)
        payments = firebase.get_collection('payments', limit=100000, include_id=True) or []
        current_year = datetime.utcnow().year

        # Compute max coveredThroughYear per user
        user_to_max_cover = {}
        for p in payments:
            try:
                if str(p.get('status', '')).lower() != 'succeeded':
                    continue
                user_id = str(p.get('userId') or '').strip()
                covered = p.get('coveredThroughYear', None)
                if not user_id or not isinstance(covered, int):
                    continue
                if user_id not in user_to_max_cover or covered > user_to_max_cover[user_id]:
                    user_to_max_cover[user_id] = covered
            except Exception:
                continue

        # Build the reconciliation set from memberships + payments
        existing_memberships = firebase.get_collection('memberships', limit=100000, include_id=True) or []
        user_ids = set([str(m.get('userId') or '').strip() for m in existing_memberships if m.get('userId')]) | set(user_to_max_cover.keys())

        # Discord env
        guild_id = os.environ.get('DISCORD_GUILD_ID')
        bot_token = os.environ.get('DISCORD_BOT_TOKEN')
        if not guild_id or not bot_token:
            return jsonify({"error": "Discord env not configured (DISCORD_GUILD_ID/DISCORD_BOT_TOKEN)"}), 500
        headers = {'Authorization': f'Bot {bot_token}'}

        result = {"updated_memberships": 0, "roles_added": 0, "roles_removed": 0, "errors": 0, "total_users": len(user_ids)}

        for uid in user_ids:
            if not uid:
                continue
            covered = user_to_max_cover.get(uid, None)
            status = 'club' if (isinstance(covered, int) and covered >= current_year) else 'community'

            # Update membership summary
            try:
                firebase.set_document('memberships', uid, {
                    "userId": uid,
                    "currentStatus": status,
                    "coveredThroughYear": covered if isinstance(covered, int) else None,
                    "updatedAt": datetime.utcnow().isoformat()
                }, merge=True)
                result["updated_memberships"] += 1
            except Exception:
                result["errors"] += 1

            # Role reconciliation
            try:
                if status == 'club':
                    # Add role
                    r = requests.put(
                        f'https://discord.com/api/v10/guilds/{guild_id}/members/{uid}/roles/{role_id}',
                        headers=headers
                    )
                    if 200 <= r.status_code < 300:
                        result["roles_added"] += 1
                    else:
                        # Still count as not-an-error if user not in guild
                        if r.status_code not in (403, 404):
                            result["errors"] += 1
                else:
                    # Remove role
                    r = requests.delete(
                        f'https://discord.com/api/v10/guilds/{guild_id}/members/{uid}/roles/{role_id}',
                        headers=headers
                    )
                    # 204 expected; 404 if user not in guild or role missing - treat as ok
                    if r.status_code in (200, 202, 204, 404):
                        result["roles_removed"] += 1
                    else:
                        result["errors"] += 1
            except Exception:
                result["errors"] += 1

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route('/login')
def login():
    """Discord OAuth login page"""
    return render_template('login.html', discord_oauth_url=DISCORD_OAUTH_URL)

@app.route('/auth/discord/callback')
def discord_callback():
    """Handle Discord OAuth callback"""
    code = request.args.get('code')
    if not code:
        flash('Authorization failed', 'error')
        return redirect(url_for('login'))
    
    try:
        # Exchange code for access token
        token_data = {
            'client_id': DISCORD_CLIENT_ID,
            'client_secret': DISCORD_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': DISCORD_REDIRECT_URI
        }
        
        token_response = requests.post('https://discord.com/api/oauth2/token', data=token_data)
        if token_response.status_code != 200:
            flash('Failed to get access token', 'error')
            return redirect(url_for('login'))
        
        token_info = token_response.json()
        access_token = token_info['access_token']
        
        # Get user info
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        user_response = requests.get('https://discord.com/api/users/@me', headers=headers)
        if user_response.status_code != 200:
            flash('Failed to get user information', 'error')
            return redirect(url_for('login'))
        
        user_info = user_response.json()
        discord_id = user_info['id']
        username = user_info['username']
        
        # Check if user has admin rights
        if not check_discord_admin(discord_id, access_token):
            flash('Access denied: You need administrator rights in the DZR Discord server', 'error')
            return redirect(url_for('login'))
        
        # Store user info in session
        session['user'] = {
            'discord_id': discord_id,
            'username': username,
            'avatar': user_info.get('avatar'),
            'access_token': access_token
        }
        session['discord_id'] = discord_id
        session['logged_in'] = True
        
        # Store user in Firebase (optional - for audit trail)
        try:
            firebase.db.collection('admin_logins').add({
                'discord_id': discord_id,
                'username': username,
                'login_time': datetime.now(),
                'ip_address': request.remote_addr
            })
        except Exception as e:
            print(f"Failed to log admin login to Firebase: {e}")
        
        flash(f'Welcome, {username}!', 'success')
        
        # Redirect to the page they were trying to access
        next_url = session.pop('next_url', None)
        if next_url:
            return redirect(next_url)
        return redirect(url_for('dashboard'))
        
    except Exception as e:
        print(f"Discord OAuth error: {e}")
        flash('Authentication failed', 'error')
        return redirect(url_for('login'))

@app.route('/logout')
def logout():
    """Logout and clear session"""
    username = session.get('user', {}).get('username', 'Unknown')
    session.clear()
    flash(f'Goodbye, {username}!', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Admin Dashboard - Overview of Discord server stats and navigation"""
    try:
        # Get Discord server stats
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        
        # Get basic server info
        server_stats = {
            'total_members': 0,
            'linked_members': 0,
            'unlinked_members': 0,
            'server_name': 'DZR Discord Server'
        }
        
        try:
            # Get all members with ZwiftIDs merged
            members = discord_api.merge_with_zwift_ids(include_role_names=True)
            server_stats['total_members'] = len(members)
            server_stats['linked_members'] = len([m for m in members if m.get('has_zwift_id')])
            server_stats['unlinked_members'] = server_stats['total_members'] - server_stats['linked_members']
        except Exception as e:
            print(f"Error getting Discord stats: {e}")
        
        # Get content stats from Firebase
        try:
            welcome_messages = firebase.get_collection('welcome_messages', limit=100, include_id=True)
            role_messages = firebase.get_collection('role_messages', limit=100, include_id=True)
            scheduled_messages = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
            
            content_stats = {
                'total_welcome': len(welcome_messages),
                'total_scheduled': len(scheduled_messages) + len(role_messages),
                'active_welcome': len([m for m in welcome_messages if m.get('active', False)]),
                'active_scheduled': len([m for m in scheduled_messages if m.get('active', False)]) + len([m for m in role_messages if m.get('active', False)])
            }
        except Exception as e:
            print(f"Error getting content stats: {e}")
            content_stats = {
                'total_welcome': 0,
                'total_scheduled': 0,
                'active_welcome': 0,
                'active_scheduled': 0
            }
        
        # Get recent admin logins
        try:
            recent_logins = firebase.get_collection('admin_logins', limit=10, include_id=True)
            # Sort by login_time if available
            recent_logins.sort(key=lambda x: x.get('login_time', datetime.min), reverse=True)
        except Exception as e:
            print(f"Error getting recent logins: {e}")
            recent_logins = []
        
        return render_template('dashboard.html', 
                             user=session.get('user', {}),
                             server_stats=server_stats,
                             content_stats=content_stats,
                             recent_logins=recent_logins[:5])  # Show only last 5
        
    except Exception as e:
        flash(f'Error loading dashboard: {str(e)}', 'error')
        return render_template('dashboard.html', 
                             user=session.get('user', {}),
                             server_stats={'total_members': 0, 'linked_members': 0, 'unlinked_members': 0},
                             content_stats={'total_welcome': 0, 'total_scheduled': 0, 'active_welcome': 0, 'active_scheduled': 0},
                             recent_logins=[])

@app.route('/api-overview')
@login_required
def api_overview():
    """Protected API Endpoint Overview"""
    return index_content()


@app.route('/bot-knowledge', methods=['GET', 'POST'])
@login_required
def bot_knowledge():
    """
    Manage admin-provided knowledge snippets used by the Discord AI assistant.
    """
    from datetime import datetime
    try:
        if request.method == 'POST':
            action = request.form.get('action', 'save')
            key = (request.form.get('key') or '').strip()
            title = (request.form.get('title') or '').strip()
            content = (request.form.get('content') or '').strip()
            tags_raw = (request.form.get('tags') or '').strip()

            if not key:
                flash('Key is required.', 'error')
            else:
                if action == 'delete':
                    ok = firebase.delete_document('bot_knowledge', key)
                    if ok:
                        flash(f'Deleted knowledge entry "{key}".', 'success')
                    else:
                        flash(f'Failed to delete knowledge entry "{key}".', 'error')
                else:
                    tags = [t.strip() for t in tags_raw.split(',') if t.strip()]
                    data = {
                        'key': key,
                        'title': title or key,
                        'content': content,
                        'tags': tags,
                        'updatedAt': datetime.utcnow(),
                    }
                    ok = firebase.set_document('bot_knowledge', key, data, merge=False)
                    if ok:
                        flash(f'Saved knowledge entry "{key}".', 'success')
                    else:
                        flash(f'Failed to save knowledge entry "{key}".', 'error')

            return redirect(url_for('bot_knowledge'))

        # GET: list all entries
        entries = firebase.get_collection('bot_knowledge', limit=200, include_id=True)
        # Sort by key
        entries.sort(key=lambda e: e.get('key') or e.get('id') or '')

        return render_template(
            'bot_knowledge.html',
            entries=entries,
            user=session.get('user', {})
        )
    except Exception as e:
        flash(f'Error loading bot knowledge: {str(e)}', 'error')
        return render_template(
            'bot_knowledge.html',
            entries=[],
            user=session.get('user', {})
        )

# Discord stats endpoints

@app.route('/api/discord/stats/summary', methods=['GET'])
@login_required
def get_discord_stats_summary():
    """Get summary statistics for Discord server activity"""
    try:
        from datetime import datetime, timedelta
        
        # Get recent activity data (last 30 days)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        
        # Convert to ISO string format to match Firebase data
        end_date_str = end_date.isoformat() + "Z"
        start_date_str = start_date.isoformat() + "Z"
        
        # Query Firebase using string timestamps
        activity_docs = firebase.db.collection('server_activity')\
            .where('timestamp', '>=', start_date_str)\
            .where('timestamp', '<=', end_date_str)\
            .order_by('timestamp', direction=firebase.firestore.Query.DESCENDING)\
            .stream()
        
        activities = [doc.to_dict() for doc in activity_docs]
        
        # Calculate summary statistics
        total_activities = sum(activity.get('totalActivities', 0) for activity in activities)
        total_messages = sum(activity.get('rawData', {}).get('messageCount', 0) for activity in activities)
        total_reactions = sum(activity.get('rawData', {}).get('reactionCount', 0) for activity in activities)
        total_voice_activity = sum(activity.get('rawData', {}).get('voiceActivityCount', 0) for activity in activities)
        total_interactions = sum(activity.get('rawData', {}).get('interactionCount', 0) for activity in activities)
        
        # Get unique users and channels
        all_users = set()
        all_channels = set()
        
        for activity in activities:
            summary = activity.get('summary', {})
            user_activity = summary.get('userActivity', {})
            channel_activity = summary.get('channelActivity', {})
            
            all_users.update(user_activity.keys())
            all_channels.update(channel_activity.keys())
        
        # Calculate daily averages
        unique_dates = set(activity.get('dateKey') for activity in activities if activity.get('dateKey'))
        days_with_data = len(unique_dates)
        avg_daily_messages = total_messages / max(days_with_data, 1)
        avg_daily_activities = total_activities / max(days_with_data, 1)
        
        summary = {
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "days": 30
            },
            "totals": {
                "activities": total_activities,
                "messages": total_messages,
                "reactions": total_reactions,
                "voice_activity": total_voice_activity,
                "interactions": total_interactions
            },
            "averages": {
                "daily_messages": round(avg_daily_messages, 1),
                "daily_activities": round(avg_daily_activities, 1)
            },
            "unique_counts": {
                "active_users": len(all_users),
                "active_channels": len(all_channels),
                "days_with_activity": days_with_data
            },
            "recent_activity_count": len(activities)
        }
        
        return jsonify(summary)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/discord/stats/daily', methods=['GET'])
@login_required
def get_daily_discord_stats():
    """Get daily activity statistics for charts"""
    try:
        days = request.args.get('days', default=30, type=int)
        
        from datetime import datetime, timedelta
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        # Convert to ISO string format to match Firebase data
        end_date_str = end_date.isoformat() + "Z"
        start_date_str = start_date.isoformat() + "Z"
        
        # Query Firebase
        activity_docs = firebase.db.collection('server_activity')\
            .where('timestamp', '>=', start_date_str)\
            .where('timestamp', '<=', end_date_str)\
            .order_by('timestamp')\
            .stream()
        
        activities = [doc.to_dict() for doc in activity_docs]
        
        # Group by dateKey
        daily_stats = {}
        
        for activity in activities:
            date_key = activity.get('dateKey')
            if not date_key:
                continue
                
            if date_key not in daily_stats:
                daily_stats[date_key] = {
                    "date": date_key,
                    "messages": 0,
                    "reactions": 0,
                    "voice_activity": 0,
                    "interactions": 0,
                    "total_activities": 0,
                    "unique_users": set(),
                    "unique_channels": set()
                }
            
            raw_data = activity.get('rawData', {})
            daily_stats[date_key]["messages"] += raw_data.get('messageCount', 0)
            daily_stats[date_key]["reactions"] += raw_data.get('reactionCount', 0)
            daily_stats[date_key]["voice_activity"] += raw_data.get('voiceActivityCount', 0)
            daily_stats[date_key]["interactions"] += raw_data.get('interactionCount', 0)
            daily_stats[date_key]["total_activities"] += activity.get('totalActivities', 0)
            
            # Track unique users and channels
            summary = activity.get('summary', {})
            user_activity = summary.get('userActivity', {})
            channel_activity = summary.get('channelActivity', {})
            
            daily_stats[date_key]["unique_users"].update(user_activity.keys())
            daily_stats[date_key]["unique_channels"].update(channel_activity.keys())
        
        # Convert sets to counts and sort by date
        result = []
        for date_key in sorted(daily_stats.keys()):
            stats = daily_stats[date_key]
            stats["unique_users"] = len(stats["unique_users"])
            stats["unique_channels"] = len(stats["unique_channels"])
            result.append(stats)
        
        return jsonify({
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "days": days
            },
            "daily_stats": result
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _record_daily_member_count_snapshot() -> None:
    """
    Record a daily snapshot of Discord member count in Firestore.
    
    Stores/updates one document per day in collection `server_member_counts`,
    using `dateKey` (YYYY-MM-DD) as the document ID.
    """
    try:
        # Use Central European Time for dateKey consistency with other dashboards
        cet = pytz.timezone('Europe/Berlin')
        now_cet = datetime.now(cet)
        date_key = now_cet.strftime('%Y-%m-%d')

        # Prefer lightweight guild counts for total; but we still enumerate members to compute role counts
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        counts = discord_api.get_guild_member_counts()
        member_count = counts.get("approximate_member_count")
        presence_count = counts.get("approximate_presence_count")

        role_counts = {}
        try:
            # Enumerate members to compute role membership counts (roleId -> count)
            all_members = discord_api.get_all_members(limit=200000, include_role_names=False)
            for m in all_members:
                for role_id in (m.get("role_ids") or []):
                    if isinstance(role_id, str) and role_id:
                        role_counts[role_id] = role_counts.get(role_id, 0) + 1
            # If lightweight count wasn't available, use enumerated count
            if not isinstance(member_count, int):
                member_count = len(all_members)
        except Exception:
            # If enumeration fails, we can still store total member count if available
            all_members = None

        if not isinstance(member_count, int):
            return

        snapshot = {
            "dateKey": date_key,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "memberCount": int(member_count),
        }
        if isinstance(presence_count, int):
            snapshot["presenceCount"] = int(presence_count)
        if isinstance(role_counts, dict) and role_counts:
            snapshot["roleCounts"] = role_counts

        firebase.db.collection('server_member_counts').document(date_key).set(snapshot, merge=True)
    except Exception as e:
        try:
            logging.warning(f"Failed to record daily member count snapshot: {e}")
        except Exception:
            pass

def _parse_discord_iso_datetime(value: str) -> Optional[datetime]:
    """
    Parse Discord ISO timestamps (often ending with 'Z') into a timezone-aware datetime.
    Returns None on parse failure.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        # Discord typically returns e.g. "2024-01-02T03:04:05.678Z"
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            # Assume UTC if tz missing
            dt = pytz.utc.localize(dt)
        return dt
    except Exception:
        return None

def _discord_snowflake_created_at_utc(snowflake_id: str) -> Optional[datetime]:
    """
    Derive creation timestamp from a Discord snowflake ID.
    Discord epoch: 2015-01-01T00:00:00.000Z
    """
    try:
        if not isinstance(snowflake_id, str) or not snowflake_id.strip():
            return None
        sf = int(snowflake_id)
        # 42-bit timestamp in ms since Discord epoch, stored in the top bits
        discord_epoch_ms = 1420070400000
        ts_ms = (sf >> 22) + discord_epoch_ms
        return datetime.fromtimestamp(ts_ms / 1000.0, tz=pytz.utc)
    except Exception:
        return None

@app.route('/api/discord/stats/members/backfill', methods=['POST'])
@login_required
def backfill_member_counts():
    """
    Estimate historical member counts for missing days using current members' joined_at timestamps.

    This is NOT a true historical reconstruction (leavers are unknown).
    It counts the cohort of members currently in the guild who had joined by each day.
    """
    try:
        body = request.get_json(silent=True) or {}
        days = body.get('days', 90)
        force = bool(body.get('force', False))
        since_creation = bool(body.get('since_creation', False))

        cet = pytz.timezone('Europe/Berlin')
        end_date = datetime.now(cet).date()

        if since_creation:
            created_at_utc = _discord_snowflake_created_at_utc(DISCORD_GUILD_ID)
            if not created_at_utc:
                return jsonify({"error": "Unable to determine server creation time from DISCORD_GUILD_ID"}), 400
            start_date = created_at_utc.astimezone(cet).date()
            days = (end_date - start_date).days + 1
        else:
            try:
                days = int(days)
            except Exception:
                days = 90
            # allow larger ranges, but keep a hard cap for safety
            days = max(1, min(days, 5000))
            start_date = end_date - timedelta(days=days - 1)

        # Hard cap safety: avoid accidentally writing extreme ranges
        if days > 5000:
            return jsonify({"error": "Requested range too large (max 5000 days)"}), 400

        # Pull current members once
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        members = discord_api.get_all_members(limit=200000, include_role_names=False)

        # Build array of join dates (CET date) for totals
        total_join_dates = []

        for m in members:
            joined_at = _parse_discord_iso_datetime(m.get('joined_at'))
            if not joined_at:
                continue
            joined_date_cet = joined_at.astimezone(cet).date()
            total_join_dates.append(joined_date_cet)

        total_join_dates.sort()

        col = firebase.db.collection('server_member_counts')
        written = 0
        skipped = 0

        # Preload existing docs once (avoids N reads for long ranges)
        existing_keys = set()
        if not force:
            try:
                start_key = start_date.strftime('%Y-%m-%d')
                end_key = end_date.strftime('%Y-%m-%d')
                existing_docs = (
                    col.where('dateKey', '>=', start_key)
                       .where('dateKey', '<=', end_key)
                       .stream()
                )
                for doc in existing_docs:
                    dct = doc.to_dict() or {}
                    dk = dct.get('dateKey')
                    if isinstance(dk, str) and dk:
                        existing_keys.add(dk)
            except Exception:
                # If this fails (e.g., permissions/index), we'll fall back to writes without skipping.
                existing_keys = set()

        # Batch writes for speed (Firestore batch limit: 500 operations)
        batch = firebase.db.batch()
        batch_ops = 0

        d = start_date
        now_utc_iso = datetime.utcnow().isoformat() + "Z"
        while d <= end_date:
            date_key = d.strftime('%Y-%m-%d')
            doc_ref = col.document(date_key)

            if not force:
                if date_key in existing_keys:
                    skipped += 1
                    d += timedelta(days=1)
                    continue

            member_count = bisect_right(total_join_dates, d)

            snapshot = {
                "dateKey": date_key,
                "timestamp": now_utc_iso,
                "memberCount": int(member_count),
                "estimated": True,
                "estimatedMode": "cohort_joined_at",
                "estimatedAt": now_utc_iso,
            }

            batch.set(doc_ref, snapshot, merge=True)
            batch_ops += 1
            written += 1

            if batch_ops >= 450:
                batch.commit()
                batch = firebase.db.batch()
                batch_ops = 0

            d += timedelta(days=1)

        if batch_ops > 0:
            batch.commit()

        return jsonify({
            "status": "ok",
            "period": {"days": days, "start": start_date.strftime('%Y-%m-%d'), "end": end_date.strftime('%Y-%m-%d')},
            "written": written,
            "skipped": skipped,
            "note": "Estimated backfill uses current members + joined_at; leavers are not represented."
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/discord/stats/members', methods=['GET'])
@login_required
def get_daily_member_counts():
    """Get daily Discord member counts for charts (y=members, x=date). Supports role filtering."""
    try:
        days = request.args.get('days', default=30, type=int)
        # Allow long ranges for backfilled totals (server creation can be multiple years)
        days = max(1, min(days, 5000))
        role_ids_param = request.args.get('role_ids', default='', type=str) or ''
        role_ids = [r.strip() for r in role_ids_param.split(',') if r.strip()]

        cet = pytz.timezone('Europe/Berlin')
        end_date = datetime.now(cet).date()
        start_date = end_date - timedelta(days=days - 1)

        start_key = start_date.strftime('%Y-%m-%d')
        end_key = end_date.strftime('%Y-%m-%d')

        docs = (
            firebase.db.collection('server_member_counts')
            .where('dateKey', '>=', start_key)
            .where('dateKey', '<=', end_key)
            .order_by('dateKey')
            .stream()
        )

        result = []
        for doc in docs:
            d = doc.to_dict() or {}
            date_key = d.get('dateKey')
            member_count = d.get('memberCount')
            if not date_key or not isinstance(member_count, int):
                continue
            row = {
                "date": date_key,
                "members": member_count,
            }
            if role_ids:
                role_counts = d.get('roleCounts') or {}
                selected = {}
                if isinstance(role_counts, dict):
                    for rid in role_ids:
                        v = role_counts.get(rid)
                        if isinstance(v, int):
                            selected[rid] = v
                row["roles"] = selected
            result.append(row)

        return jsonify({
            "period": {"days": days, "start": start_key, "end": end_key},
            "daily_members": result
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/discord/stats/roles', methods=['GET'])
@login_required
def get_discord_roles_for_stats():
    """Return guild roles for role selection in the stats UI."""
    try:
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        roles_lookup = discord_api.get_guild_roles() or {}
        roles = []
        for role_id, role in roles_lookup.items():
            name = role.get("name")
            if not isinstance(role_id, str) or not role_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            # Hide @everyone from UI; it matches guild id and isn't useful as a filter
            if name == "@everyone":
                continue
            roles.append({"id": role_id, "name": name})
        roles.sort(key=lambda r: r["name"].lower())
        return jsonify({"roles": roles})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/discord/stats/top-users', methods=['GET'])
@login_required
def get_top_users():
    """Get most active users"""
    try:
        days = request.args.get('days', default=30, type=int)
        limit = request.args.get('limit', default=10, type=int)
        
        from datetime import datetime, timedelta
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        # Convert to ISO string format
        end_date_str = end_date.isoformat() + "Z"
        start_date_str = start_date.isoformat() + "Z"
        
        # Query Firebase
        activity_docs = firebase.db.collection('server_activity')\
            .where('timestamp', '>=', start_date_str)\
            .where('timestamp', '<=', end_date_str)\
            .stream()
        
        activities = [doc.to_dict() for doc in activity_docs]
        
        # Aggregate user activity
        user_totals = {}
        
        for activity in activities:
            summary = activity.get('summary', {})
            user_activity = summary.get('userActivity', {})
            
            for user_id, user_data in user_activity.items():
                if user_id not in user_totals:
                    user_totals[user_id] = {
                        "user_id": user_id,
                        "username": user_data.get('username', 'Unknown'),
                        "messages": 0,
                        "reactions": 0,
                        "voice_activity": 0,
                        "interactions": 0,
                        "total_activities": 0
                    }
                
                user_totals[user_id]["messages"] += user_data.get('messages', 0)
                user_totals[user_id]["reactions"] += user_data.get('reactions', 0)
                user_totals[user_id]["voice_activity"] += user_data.get('voiceActivity', 0)
                user_totals[user_id]["interactions"] += user_data.get('interactions', 0)
                user_totals[user_id]["total_activities"] += (
                    user_data.get('messages', 0) + 
                    user_data.get('reactions', 0) + 
                    user_data.get('voiceActivity', 0) + 
                    user_data.get('interactions', 0)
                )
        
        # Convert to list and sort by total activities
        top_users = list(user_totals.values())
        top_users.sort(key=lambda x: x["total_activities"], reverse=True)
        
        return jsonify({
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "days": days
            },
            "top_users": top_users[:limit]
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/discord/stats/top-channels', methods=['GET'])
@login_required
def get_top_channels():
    """Get most active channels"""
    try:
        days = request.args.get('days', default=30, type=int)
        limit = request.args.get('limit', default=10, type=int)
        
        from datetime import datetime, timedelta
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        # Convert to ISO string format
        end_date_str = end_date.isoformat() + "Z"
        start_date_str = start_date.isoformat() + "Z"
        
        # Query Firebase
        activity_docs = firebase.db.collection('server_activity')\
            .where('timestamp', '>=', start_date_str)\
            .where('timestamp', '<=', end_date_str)\
            .stream()
        
        activities = [doc.to_dict() for doc in activity_docs]
        
        # Aggregate channel activity
        channel_totals = {}
        
        for activity in activities:
            summary = activity.get('summary', {})
            channel_activity = summary.get('channelActivity', {})
            
            for channel_id, channel_data in channel_activity.items():
                if channel_id not in channel_totals:
                    channel_totals[channel_id] = {
                        "channel_id": channel_id,
                        "channel_name": channel_data.get('channelName', 'Unknown'),
                        "messages": 0,
                        "reactions": 0,
                        "total_activities": 0
                    }
                
                channel_totals[channel_id]["messages"] += channel_data.get('messages', 0)
                channel_totals[channel_id]["reactions"] += channel_data.get('reactions', 0)
                channel_totals[channel_id]["total_activities"] += (
                    channel_data.get('messages', 0) + 
                    channel_data.get('reactions', 0)
                )
        
        # Convert to list and sort by total activities
        top_channels = list(channel_totals.values())
        top_channels.sort(key=lambda x: x["total_activities"], reverse=True)
        
        return jsonify({
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "days": days
            },
            "top_channels": top_channels[:limit]
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Main Discord stats page
@app.route('/discord/stats', methods=['GET'])
@login_required
def discord_stats():
    """Discord server statistics dashboard"""
    _record_daily_member_count_snapshot()
    return render_template('discord_stats.html')

# Optional debug endpoint to verify data exists
@app.route('/api/discord/stats/debug', methods=['GET'])
@login_required
def debug_discord_stats():
    """Debug endpoint to check what data exists in Firebase"""
    try:
        # Get recent documents
        activity_docs = firebase.db.collection('server_activity')\
            .order_by('timestamp', direction=firebase.firestore.Query.DESCENDING)\
            .limit(5)\
            .stream()
        
        documents = []
        for doc in activity_docs:
            doc_data = doc.to_dict()
            documents.append({
                'id': doc.id,
                'timestamp': doc_data.get('timestamp'),
                'dateKey': doc_data.get('dateKey'),
                'totalActivities': doc_data.get('totalActivities'),
                'rawData': doc_data.get('rawData')
            })
        
        return jsonify({
            'total_documents': len(documents),
            'documents': documents
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/', methods=['GET'])
def index():
    """
    Root page - redirects to dashboard if logged in, otherwise redirects to login
    """
    # If user is logged in, redirect to dashboard
    if 'user' in session and 'discord_id' in session:
        return redirect(url_for('dashboard'))
    
    # Otherwise redirect to login
    return redirect(url_for('login'))

def index_content():
    """
    API Endpoint Overview - Lists all available endpoints in this service
    """
    output = []
    for rule in app.url_map.iter_rules():
        # Skip static file routes and the index route itself
        if rule.endpoint in ['static', 'index']:
            continue

        view_func = app.view_functions[rule.endpoint]
        doc = inspect.getdoc(view_func) or "No description available"
        
        # Get first line of docstring for description, clean it up
        description = doc.split("\n")[0].strip()
        
        # Clean up the path - remove angle brackets for better readability in examples
        example_path = rule.rule
        methods = sorted(rule.methods - {'OPTIONS', 'HEAD'})
        
        # Add some example parameter values for paths with variables
        if '<int:club_id>' in example_path:
            example_path = example_path.replace('<int:club_id>', '11939')
        if '<int:rider_id>' in example_path:
            example_path = example_path.replace('<int:rider_id>', '15690')
        if '<club_id>' in example_path:
            example_path = example_path.replace('<club_id>', '11939')
        if '<rider_id>' in example_path:
            example_path = example_path.replace('<rider_id>', '15690')

        output.append({
            "endpoint": rule.rule,
            "methods": methods,
            "description": description,
            "example": example_path
        })

    # Sort by endpoint path for better organization
    output.sort(key=lambda x: x['endpoint'])
    
    # Check if request wants HTML (browser) or JSON (API client)
    if request.args.get('format') == 'json' or request.headers.get('Accept', '').find('text/html') < 0:
        return jsonify({
            "service": "Zwift Power API", 
            "version": "1.0",
            "total_endpoints": len(output),
            "endpoints": output
        })
    else:
        return render_template('api_overview.html', endpoints=output)

@app.route('/api/schedules/probability-due', methods=['GET'])
def get_probability_due_messages():
    """Get messages that should be considered for probability-based sending today"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Get all active probability-based schedules
        schedules = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
        
        probability_messages = []
        from datetime import datetime, timezone
        import pytz
        
        # Use Central European Time for consistency
        cet = pytz.timezone('Europe/Berlin')
        current_time = datetime.now(cet)
        current_date = current_time.date()
        
        print(f"[DEBUG] Checking for probability-based messages for date {current_date}")
        
        for schedule in schedules:
            if not schedule.get('active', False):
                continue
            
            # Only process probability-based schedules
            schedule_config = schedule.get('schedule', {})
            if schedule_config.get('type') != 'probability':
                continue
            
            # Check if we've already sent a message today
            last_sent = schedule.get('last_sent')
            if last_sent:
                # Convert to date for comparison
                if hasattr(last_sent, 'seconds'):
                    # Firebase Timestamp
                    last_sent_date = datetime.fromtimestamp(last_sent.seconds, tz=cet).date()
                elif isinstance(last_sent, datetime):
                    if last_sent.tzinfo is None:
                        last_sent_date = cet.localize(last_sent).date()
                    else:
                        last_sent_date = last_sent.astimezone(cet).date()
                else:
                    try:
                        last_sent_datetime = datetime.fromisoformat(str(last_sent))
                        if last_sent_datetime.tzinfo is None:
                            last_sent_date = cet.localize(last_sent_datetime).date()
                        else:
                            last_sent_date = last_sent_datetime.astimezone(cet).date()
                    except:
                        last_sent_date = None
                
                # Skip if already sent today
                if last_sent_date == current_date:
                    print(f"[DEBUG] Schedule {schedule.get('id', 'unknown')} already sent today")
                    continue
            
            print(f"[DEBUG] Schedule {schedule.get('id', 'unknown')} is eligible for probability check")
            probability_messages.append(schedule)
        
        print(f"[DEBUG] Found {len(probability_messages)} probability-eligible messages")
        return jsonify(probability_messages)
    except Exception as e:
        print(f"Error in get_probability_due_messages: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/schedules/probability-check', methods=['POST'])
def check_probability_and_select():
    """Check if messages should be sent based on probability and select which ones"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        import random
        
        # Get probability-eligible messages by calling the logic directly
        try:
            # Get all active probability-based schedules
            schedules = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
            
            probability_messages = []
            from datetime import datetime, timezone
            import pytz
            
            # Use Central European Time for consistency
            cet = pytz.timezone('Europe/Berlin')
            current_time = datetime.now(cet)
            current_date = current_time.date()
            
            print(f"[DEBUG] Checking for probability-based messages for date {current_date}")
            
            for schedule in schedules:
                if not schedule.get('active', False):
                    continue
                
                # Only process probability-based schedules
                schedule_config = schedule.get('schedule', {})
                if schedule_config.get('type') != 'probability':
                    continue
                
                # Check if we've already sent a message today
                last_sent = schedule.get('last_sent')
                if last_sent:
                    # Convert to date for comparison
                    if hasattr(last_sent, 'seconds'):
                        # Firebase Timestamp
                        last_sent_date = datetime.fromtimestamp(last_sent.seconds, tz=cet).date()
                    elif isinstance(last_sent, datetime):
                        if last_sent.tzinfo is None:
                            last_sent_date = cet.localize(last_sent).date()
                        else:
                            last_sent_date = last_sent.astimezone(cet).date()
                    else:
                        try:
                            last_sent_datetime = datetime.fromisoformat(str(last_sent))
                            if last_sent_datetime.tzinfo is None:
                                last_sent_date = cet.localize(last_sent_datetime).date()
                            else:
                                last_sent_date = last_sent_datetime.astimezone(cet).date()
                        except:
                            last_sent_date = None
                    
                    # Skip if already sent today
                    if last_sent_date == current_date:
                        print(f"[DEBUG] Schedule {schedule.get('id', 'unknown')} already sent today")
                        continue
                
                print(f"[DEBUG] Schedule {schedule.get('id', 'unknown')} is eligible for probability check")
                probability_messages.append(schedule)
            
            eligible_messages = probability_messages
            
        except Exception as e:
            print(f"Error getting probability eligible messages: {str(e)}")
            return jsonify({"error": f"Failed to get eligible messages: {str(e)}"}), 500
        
        if not eligible_messages:
            return jsonify({"messages_to_send": [], "total_eligible": 0})
        
        # Get global daily probability setting
        global_settings = firebase.get_document('system_settings', 'global')
        global_daily_probability = global_settings.get('daily_probability', 0.2) if global_settings else 0.2
        
        print(f"[DEBUG] Using global daily probability: {global_daily_probability}")
        print(f"[DEBUG] Total eligible messages across all channels: {len(eligible_messages)}")
        
        # Single probability roll for ALL messages
        if random.random() < global_daily_probability:
            print(f"[DEBUG] Global probability roll succeeded (p={global_daily_probability})")
            
            # Select ONE message from ALL eligible messages based on likelihood weights
            weights = []
            for msg in eligible_messages:
                likelihood = msg.get('schedule', {}).get('likelihood', 1.0)
                weights.append(likelihood)
            
            # Weighted random selection from all messages
            if weights and sum(weights) > 0:
                selected_message = random.choices(eligible_messages, weights=weights)[0]
                print(f"[DEBUG] Selected message: {selected_message.get('title', 'Unknown')} (channel: {selected_message.get('channel_id')}, likelihood={selected_message.get('schedule', {}).get('likelihood', 1.0)})")
                messages_to_send = [selected_message]
            else:
                # Fallback to random selection if no weights
                selected_message = random.choice(eligible_messages)
                print(f"[DEBUG] Selected message (no weights): {selected_message.get('title', 'Unknown')} (channel: {selected_message.get('channel_id')})")
                messages_to_send = [selected_message]
        else:
            print(f"[DEBUG] Global probability roll failed (p={global_daily_probability})")
            messages_to_send = []
        
        # Count unique channels for reporting
        unique_channels = len(set(msg.get('channel_id') for msg in eligible_messages))
        
        return jsonify({
            "messages_to_send": messages_to_send,
            "total_eligible": len(eligible_messages),
            "channels_with_eligible_messages": unique_channels,
            "global_daily_probability": global_daily_probability,
            "selection_method": "global_probability_single_selection"
        })
        
    except Exception as e:
        print(f"Error in check_probability_and_select: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/roles', methods=['GET'])
@login_required
def roles_overview():
    """Role management overview page"""
    return render_template('roles_overview.html')

@app.route('/api/roles/panels', methods=['GET'])
@login_required
def get_role_panels():
    """Get all role panels for the guild"""
    try:
        # Get panels from Firebase (using the same structure as the bot)
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        print(f"[DEBUG] Fetched panels_doc: {panels_doc}")
        
        if not panels_doc or 'panels' not in panels_doc:
            return jsonify({"panels": []})
        
        # Get Discord API instance to fetch role information
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        guild_roles = discord_api.get_guild_roles()
        
        # Format panels with role information
        formatted_panels = []
        for panel_id, panel_data in panels_doc['panels'].items():
            print(f"[DEBUG] Processing panel {panel_id}: {panel_data}")
            # Add role details to each role in the panel
            roles_with_details = []
            for role in panel_data.get('roles', []):
                role_id = role['roleId']
                role_details = guild_roles.get(role_id, {})
                roles_with_details.append({
                    **role,
                    'roleName': role_details.get('name', role.get('roleName', 'Unknown Role')),
                    'roleColor': role_details.get('color', 0),
                    'rolePosition': role_details.get('position', 0),
                    'roleExists': role_id in guild_roles
                })
            
            formatted_panels.append({
                'panelId': panel_id,
                'name': panel_data.get('name', 'Unnamed Panel'),
                'description': panel_data.get('description', ''),
                'channelId': panel_data.get('channelId'),
                'roles': roles_with_details,
                'requiredRoles': panel_data.get('requiredRoles', []),
                'approvalChannelId': panel_data.get('approvalChannelId'),
                'createdAt': panel_data.get('createdAt'),
                'updatedAt': panel_data.get('updatedAt'),
                'order': panel_data.get('order', 0)
            })
        
        # Sort panels by order
        formatted_panels.sort(key=lambda x: x['order'])
        
        print(f"[DEBUG] Returning formatted_panels: {formatted_panels}")
        return jsonify({"panels": formatted_panels})
        
    except Exception as e:
        print(f"Error fetching role panels: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/guild-roles', methods=['GET'])
@login_required
def get_guild_roles():
    """Get all guild roles for role selection"""
    try:
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        guild_roles = discord_api.get_guild_roles()
        
        # Filter out managed roles and @everyone
        filtered_roles = []
        for role_id, role_data in guild_roles.items():
            if not role_data.get('managed', False) and role_data.get('name') != '@everyone':
                filtered_roles.append({
                    'id': role_id,
                    'name': role_data.get('name', 'Unknown Role'),
                    'color': role_data.get('color', 0),
                    'position': role_data.get('position', 0),
                    'permissions': role_data.get('permissions', '0')
                })
        
        # Sort by position (higher position = higher in hierarchy)
        filtered_roles.sort(key=lambda x: x['position'], reverse=True)
        
        return jsonify({"roles": filtered_roles})
        
    except Exception as e:
        print(f"Error fetching guild roles: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/panels', methods=['POST'])
@login_required
def create_role_panel():
    """Create a new role panel"""
    try:
        data = request.get_json()
        
        required_fields = ['panelId', 'channelId', 'name']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        panel_id = data['panelId']
        channel_id = data['channelId']
        name = data['name']
        description = data.get('description', 'Click the buttons below to add or remove roles!')
        required_roles = data.get('requiredRoles', [])
        approval_channel_id = data.get('approvalChannelId')
        
        # Get existing panels document
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID) or {"panels": {}}
        
        # Check if panel ID already exists
        if panel_id in panels_doc.get('panels', {}):
            return jsonify({"error": "Panel ID already exists"}), 400
        
        # Use regular datetime instead of SERVER_TIMESTAMP
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        # Create new panel
        new_panel = {
            'channelId': channel_id,
            'name': name,
            'description': description,
            'roles': [],
            'panelMessageId': None,
            'requiredRoles': required_roles,
            'approvalChannelId': approval_channel_id,
            'order': len(panels_doc.get('panels', {})) + 1,  
            'createdAt': now.isoformat(),
            'updatedAt': now.isoformat()
        }
        
        # Add to panels
        if 'panels' not in panels_doc:
            panels_doc['panels'] = {}
        panels_doc['panels'][panel_id] = new_panel
        panels_doc['updatedAt'] = now.isoformat()
        
        # Save to Firebase
        firebase.set_document("selfRoles", DISCORD_GUILD_ID, panels_doc)
        
        return jsonify({"success": True, "message": f"Panel '{name}' created successfully"})
        
    except Exception as e:
        print(f"Error creating role panel: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/panels/<panel_id>', methods=['PUT'])
@login_required  
def update_role_panel(panel_id):
    """Update an existing role panel"""
    try:
        data = request.get_json()
        
        # Get existing panels document
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        if not panels_doc or 'panels' not in panels_doc or panel_id not in panels_doc['panels']:
            return jsonify({"error": "Panel not found"}), 404
        
        panel = panels_doc['panels'][panel_id]
        
        # Update fields
        if 'name' in data:
            panel['name'] = data['name']
        if 'description' in data:
            panel['description'] = data['description']
        if 'channelId' in data:
            panel['channelId'] = data['channelId']
        if 'requiredRoles' in data:
            panel['requiredRoles'] = data['requiredRoles']
        if 'approvalChannelId' in data:
            panel['approvalChannelId'] = data['approvalChannelId']
        
        # Use regular datetime instead of SERVER_TIMESTAMP
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        panel['updatedAt'] = now.isoformat()
        panels_doc['updatedAt'] = now.isoformat()
        
        # Save to Firebase
        firebase.set_document("selfRoles", DISCORD_GUILD_ID, panels_doc)
        
        return jsonify({"success": True, "message": "Panel updated successfully"})
        
    except Exception as e:
        print(f"Error updating role panel: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/panels/<panel_id>', methods=['DELETE'])
@login_required
def delete_role_panel(panel_id):
    """Delete a role panel"""
    try:
        # Get existing panels document
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        if not panels_doc or 'panels' not in panels_doc or panel_id not in panels_doc['panels']:
            return jsonify({"error": "Panel not found"}), 404
        
        # Remove panel
        del panels_doc['panels'][panel_id]
        
        # Use regular datetime instead of SERVER_TIMESTAMP
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        panels_doc['updatedAt'] = now.isoformat()
        
        # Save to Firebase
        firebase.set_document("selfRoles", DISCORD_GUILD_ID, panels_doc)
        
        return jsonify({"success": True, "message": "Panel deleted successfully"})
        
    except Exception as e:
        print(f"Error deleting role panel: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/panels/<panel_id>/roles', methods=['POST'])
@login_required
def add_role_to_panel(panel_id):
    """Add a role to a specific panel"""
    try:
        data = request.get_json()
        print(f"[DEBUG] Adding role to panel {panel_id}: {data}")
        
        required_fields = ['roleId', 'roleName']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Get existing panels document
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        print(f"[DEBUG] Current panels_doc before adding role: {panels_doc}")
        
        if not panels_doc or 'panels' not in panels_doc or panel_id not in panels_doc['panels']:
            return jsonify({"error": "Panel not found"}), 404
        
        panel = panels_doc['panels'][panel_id]
        print(f"[DEBUG] Current panel before adding role: {panel}")
        
        # Check if role already exists in panel
        role_id = data['roleId']
        if any(role['roleId'] == role_id for role in panel['roles']):
            return jsonify({"error": "Role already exists in this panel"}), 400
        
        # Add role to panel - use regular datetime instead of SERVER_TIMESTAMP
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        new_role = {
            'roleId': role_id,
            'roleName': data['roleName'],
            'description': data.get('description'),
            'emoji': data.get('emoji'),
            'requiresApproval': data.get('requiresApproval', False),
            'teamCaptainId': data.get('teamCaptainId'),
            'roleApprovalChannelId': data.get('roleApprovalChannelId'),
            'buttonColor': data.get('buttonColor', 'Secondary'),
            'requiredRoles': data.get('requiredRoles', []),
            'addedAt': now.isoformat(),
            # Team metadata (optional; only for team roles)
            'isTeamRole': bool(data.get('isTeamRole', False)),
            'teamName': data.get('teamName'),
            'raceSeries': data.get('raceSeries'),
            'division': data.get('division'),
            'rideTime': data.get('rideTime'),
            'lookingForRiders': bool(data.get('lookingForRiders', False)),
            'sortIndex': data.get('sortIndex', 0),
            'visibility': data.get('visibility', 'public'),
            'captainDisplayName': data.get('captainDisplayName')
        }
        
        print(f"[DEBUG] New role to add: {new_role}")
        panel['roles'].append(new_role)
        panel['updatedAt'] = now.isoformat()
        panels_doc['updatedAt'] = now.isoformat()
        
        print(f"[DEBUG] Panel after adding role: {panel}")
        print(f"[DEBUG] Full panels_doc before saving: {panels_doc}")
        
        # Save to Firebase
        result = firebase.set_document("selfRoles", DISCORD_GUILD_ID, panels_doc)
        print(f"[DEBUG] Firebase save result: {result}")
        
        # Verify the save by reading it back
        verification_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        print(f"[DEBUG] Verification read after save: {verification_doc}")
        
        return jsonify({"success": True, "message": f"Role '{data['roleName']}' added to panel"})
        
    except Exception as e:
        print(f"Error adding role to panel: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/panels/<panel_id>/roles/<role_id>', methods=['DELETE'])
@login_required
def remove_role_from_panel(panel_id, role_id):
    """Remove a role from a specific panel"""
    try:
        # Get existing panels document
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        if not panels_doc or 'panels' not in panels_doc or panel_id not in panels_doc['panels']:
            return jsonify({"error": "Panel not found"}), 404
        
        panel = panels_doc['panels'][panel_id]
        
        # Find and remove role
        original_length = len(panel['roles'])
        panel['roles'] = [role for role in panel['roles'] if role['roleId'] != role_id]
        
        if len(panel['roles']) == original_length:
            return jsonify({"error": "Role not found in panel"}), 404
        
        # Use regular datetime instead of SERVER_TIMESTAMP
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        panel['updatedAt'] = now.isoformat()
        panels_doc['updatedAt'] = now.isoformat()
        
        # Save to Firebase
        firebase.set_document("selfRoles", DISCORD_GUILD_ID, panels_doc)
        
        return jsonify({"success": True, "message": "Role removed from panel"})
        
    except Exception as e:
        print(f"Error removing role from panel: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/discord/channels', methods=['GET'])
@login_required  
def get_discord_channels():
    """Get all Discord text channels"""
    try:
        discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
        
        # Get guild channels using Discord API
        headers = {
            'Authorization': f'Bot {discord_api.bot_token}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get(
            f'https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/channels',
            headers=headers
        )
        
        if response.status_code != 200:
            return jsonify({"error": "Failed to fetch Discord channels"}), 500
        
        all_channels = response.json()
        
        # Filter for text channels only
        text_channels = []
        for channel in all_channels:
            if channel.get('type') == 0:  # 0 = GUILD_TEXT
                text_channels.append({
                    'id': channel['id'],
                    'name': channel['name'],
                    'position': channel.get('position', 0),
                    'parent_id': channel.get('parent_id'),
                    'nsfw': channel.get('nsfw', False)
                })
        
        # Sort by position
        text_channels.sort(key=lambda x: x['position'])
        
        return jsonify({"channels": text_channels})
        
    except Exception as e:
        print(f"Error fetching Discord channels: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/debug', methods=['GET'])
@login_required
def debug_roles():
    """Debug endpoint to check what's actually stored in Firebase for roles"""
    try:
        # Get the raw document from Firebase
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        
        return jsonify({
            "raw_firebase_data": panels_doc,
            "guild_id": DISCORD_GUILD_ID,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        print(f"Error in debug endpoint: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/team-roles', methods=['GET'])
@login_required
def list_team_roles():
    """List roles marked as team roles, with team metadata."""
    try:
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        roles = []
        if panels_doc and 'panels' in panels_doc:
            for panel_id, panel in panels_doc['panels'].items():
                for role in panel.get('roles', []):
                    if role.get('isTeamRole'):
                        roles.append({
                            'panelId': panel_id,
                            'roleId': role.get('roleId'),
                            'roleName': role.get('roleName'),
                            'teamName': role.get('teamName') or role.get('roleName'),
                            'raceSeries': role.get('raceSeries'),
                            'division': role.get('division'),
                            'rideTime': role.get('rideTime'),
                            'lookingForRiders': role.get('lookingForRiders', False),
                            'sortIndex': role.get('sortIndex', 0),
                            'visibility': role.get('visibility', 'public'),
                            'teamCaptainId': role.get('teamCaptainId'),
                            'captainDisplayName': role.get('captainDisplayName')
                        })
        # Optional sort by sortIndex then by teamName
        roles.sort(key=lambda r: (r.get('sortIndex') or 0, (r.get('teamName') or '').lower()))
        return jsonify({ 'roles': roles })
    except Exception as e:
        print(f"Error listing team roles: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/panels/<panel_id>/roles/<role_id>', methods=['PUT'])
@login_required
def update_role_in_panel(panel_id, role_id):
    """Update a role in a specific panel"""
    try:
        data = request.get_json()
        print(f"[DEBUG] Updating role {role_id} in panel {panel_id}: {data}")
        
        # Get existing panels document
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        if not panels_doc or 'panels' not in panels_doc or panel_id not in panels_doc['panels']:
            return jsonify({"error": "Panel not found"}), 404
        
        panel = panels_doc['panels'][panel_id]
        
        # Find the role to update
        role_to_update = None
        for i, role in enumerate(panel['roles']):
            if role['roleId'] == role_id:
                role_to_update = role
                break
        
        if not role_to_update:
            return jsonify({"error": "Role not found in panel"}), 404
        
        # Update role fields
        if 'description' in data:
            role_to_update['description'] = data['description']
        if 'emoji' in data:
            role_to_update['emoji'] = data['emoji']
        if 'requiresApproval' in data:
            role_to_update['requiresApproval'] = data['requiresApproval']
        if 'teamCaptainId' in data:
            role_to_update['teamCaptainId'] = data['teamCaptainId']
        if 'roleApprovalChannelId' in data:
            role_to_update['roleApprovalChannelId'] = data['roleApprovalChannelId']
        if 'buttonColor' in data:
            # Validate button color
            valid_colors = ['Primary', 'Secondary', 'Success', 'Danger']
            if data['buttonColor'] in valid_colors:
                role_to_update['buttonColor'] = data['buttonColor']
        if 'requiredRoles' in data:
            # Validate required roles (should be an array of role IDs)
            required_roles = data['requiredRoles']
            if isinstance(required_roles, list):
                role_to_update['requiredRoles'] = required_roles
        # Team metadata fields (optional)
        if 'isTeamRole' in data:
            role_to_update['isTeamRole'] = bool(data['isTeamRole'])
        if 'teamName' in data:
            role_to_update['teamName'] = data['teamName']
        if 'raceSeries' in data:
            role_to_update['raceSeries'] = data['raceSeries']
        if 'division' in data:
            role_to_update['division'] = data['division']
        if 'rideTime' in data:
            role_to_update['rideTime'] = data['rideTime']
        if 'lookingForRiders' in data:
            role_to_update['lookingForRiders'] = bool(data['lookingForRiders'])
        if 'sortIndex' in data:
            role_to_update['sortIndex'] = data['sortIndex']
        if 'visibility' in data:
            role_to_update['visibility'] = data['visibility']
        if 'captainDisplayName' in data:
            role_to_update['captainDisplayName'] = data['captainDisplayName']
        
        # Update timestamps
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        role_to_update['updatedAt'] = now.isoformat()
        panel['updatedAt'] = now.isoformat()
        panels_doc['updatedAt'] = now.isoformat()
        
        # Save to Firebase
        result = firebase.set_document("selfRoles", DISCORD_GUILD_ID, panels_doc)
        print(f"[DEBUG] Firebase save result: {result}")
        
        return jsonify({"success": True, "message": f"Role '{role_to_update['roleName']}' updated successfully"})
        
    except Exception as e:
        print(f"Error updating role in panel: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/panels/<panel_id>/reorder', methods=['PUT'])
@login_required
def reorder_panel_roles(panel_id):
    """Reorder roles within a specific panel"""
    try:
        data = request.get_json()
        print(f"[DEBUG] Reordering roles in panel {panel_id}: {data}")
        
        if 'roleOrder' not in data:
            return jsonify({"error": "Missing roleOrder in request"}), 400
        
        role_order = data['roleOrder']
        if not isinstance(role_order, list):
            return jsonify({"error": "roleOrder must be an array"}), 400
        
        # Get existing panels document
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        if not panels_doc or 'panels' not in panels_doc or panel_id not in panels_doc['panels']:
            return jsonify({"error": "Panel not found"}), 404
        
        panel = panels_doc['panels'][panel_id]
        existing_roles = panel['roles']
        
        # Validate that all role IDs in the order exist
        existing_role_ids = {role['roleId'] for role in existing_roles}
        provided_role_ids = set(role_order)
        
        if existing_role_ids != provided_role_ids:
            missing = existing_role_ids - provided_role_ids
            extra = provided_role_ids - existing_role_ids
            error_msg = f"Role ID mismatch. Missing: {missing}, Extra: {extra}"
            return jsonify({"error": error_msg}), 400
        
        # Create a mapping of roleId to role object
        role_map = {role['roleId']: role for role in existing_roles}
        
        # Reorder the roles according to the new order
        reordered_roles = [role_map[role_id] for role_id in role_order]
        
        # Update the panel with the new order
        panel['roles'] = reordered_roles
        
        # Update timestamps
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        panel['updatedAt'] = now.isoformat()
        panels_doc['updatedAt'] = now.isoformat()
        
        # Save to Firebase
        result = firebase.set_document("selfRoles", DISCORD_GUILD_ID, panels_doc)
        print(f"[DEBUG] Firebase save result: {result}")
        
        return jsonify({"success": True, "message": f"Role order updated successfully"})
        
    except Exception as e:
        print(f"Error reordering panel roles: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/roles/panels/<panel_id>/roles/<role_id>/prerequisites', methods=['PUT'])
@login_required
def update_role_prerequisites(panel_id, role_id):
    """Update role prerequisites for a specific role"""
    try:
        data = request.get_json()
        print(f"[DEBUG] Updating prerequisites for role {role_id} in panel {panel_id}: {data}")
        
        if 'requiredRoles' not in data:
            return jsonify({"error": "Missing requiredRoles in request"}), 400
        
        required_roles = data['requiredRoles']
        if not isinstance(required_roles, list):
            return jsonify({"error": "requiredRoles must be an array"}), 400
        
        # Get existing panels document
        panels_doc = firebase.get_document("selfRoles", DISCORD_GUILD_ID)
        if not panels_doc or 'panels' not in panels_doc or panel_id not in panels_doc['panels']:
            return jsonify({"error": "Panel not found"}), 404
        
        panel = panels_doc['panels'][panel_id]
        
        # Find the role to update
        role_to_update = None
        for role in panel['roles']:
            if role['roleId'] == role_id:
                role_to_update = role
                break
        
        if not role_to_update:
            return jsonify({"error": "Role not found in panel"}), 404
        
        # Update role prerequisites
        role_to_update['requiredRoles'] = required_roles
        
        # Update timestamps
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        role_to_update['updatedAt'] = now.isoformat()
        panel['updatedAt'] = now.isoformat()
        panels_doc['updatedAt'] = now.isoformat()
        
        # Save to Firebase
        result = firebase.set_document("selfRoles", DISCORD_GUILD_ID, panels_doc)
        print(f"[DEBUG] Firebase save result: {result}")
        
        prerequisite_names = []
        if required_roles:
            # Try to get role names for better response
            try:
                discord_api = DiscordAPI(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID)
                headers = {
                    'Authorization': f'Bot {discord_api.bot_token}',
                    'Content-Type': 'application/json'
                }
                
                response = requests.get(
                    f'https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/roles',
                    headers=headers
                )
                
                if response.status_code == 200:
                    guild_roles = response.json()
                    role_name_map = {role['id']: role['name'] for role in guild_roles}
                    prerequisite_names = [role_name_map.get(req_role_id, req_role_id) for req_role_id in required_roles]
            except Exception as role_fetch_error:
                print(f"Warning: Could not fetch role names: {role_fetch_error}")
                prerequisite_names = required_roles
        
        message = f"Prerequisites updated for role '{role_to_update['roleName']}'"
        if prerequisite_names:
            message += f". Required roles: {', '.join(prerequisite_names)}"
        else:
            message += ". No prerequisites required."
        
        return jsonify({"success": True, "message": message})
        
    except Exception as e:
        print(f"Error updating role prerequisites: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/settings/global', methods=['GET'])
def get_global_settings():
    """Get global system settings"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Get global settings document from Firebase
        settings = firebase.get_document('system_settings', 'global')
        
        if not settings:
            # Return default settings if none exist
            return jsonify({
                "daily_probability": 0.2
            })
        
        return jsonify(settings)
        
    except Exception as e:
        print(f"Error getting global settings: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/settings/global', methods=['POST'])
def save_global_settings():
    """Save global system settings"""
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.get_json()
        
        # Validate daily_probability
        daily_probability = data.get('daily_probability')
        if daily_probability is None or not (0 <= daily_probability <= 1):
            return jsonify({"error": "daily_probability must be between 0.0 and 1.0"}), 400
        
        # Save to Firebase
        settings = {
            'daily_probability': daily_probability,
            'updated_at': datetime.now(pytz.timezone('Europe/Berlin')).isoformat()
        }
        
        firebase.set_document('system_settings', 'global', settings)
        
        return jsonify({"message": "Global settings saved successfully"})
        
    except Exception as e:
        print(f"Error saving global settings: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/zwift/club/roster/refresh', methods=['POST'])
def refresh_default_zwift_club_roster():
    """
    Refresh Zwift club roster for the single configured club (ZWIFT_CLUB_ID) and store it in Firestore.

    Auth: Bearer CONTENT_API_KEY
    """
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    if not ZWIFT_CLUB_ID or not str(ZWIFT_CLUB_ID).strip():
        return jsonify({"error": "ZWIFT_CLUB_ID is not configured"}), 400

    try:
        limit = int(request.args.get("limit", "100"))
        paginate_raw = str(request.args.get("paginate", "true")).lower().strip()
        paginate = paginate_raw not in ("0", "false", "no", "off")

        payload = _refresh_companion_club_roster(str(ZWIFT_CLUB_ID), limit=limit, paginate=paginate)
        return jsonify(payload)

    except Exception as e:
        print(f"Error refreshing default Zwift club roster: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/zwiftpower/club/roster/refresh', methods=['POST'])
def refresh_zwiftpower_club_roster():
    """
    Refresh ZwiftPower team_riders roster for the configured club (ZWIFTPOWER_CLUB_ID)
    and store it in Firestore.

    Auth: Bearer CONTENT_API_KEY

    Overwrites Firestore collection: zwiftpower_club_members
    """
    if not verify_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    if not ZWIFTPOWER_CLUB_ID or not str(ZWIFTPOWER_CLUB_ID).strip():
        return jsonify({"error": "ZWIFTPOWER_CLUB_ID is not configured"}), 400

    club_id_str = str(ZWIFTPOWER_CLUB_ID or "").strip()

    try:
        club_id = int(club_id_str)
    except Exception:
        return jsonify({"error": "ZWIFTPOWER_CLUB_ID must be an integer"}), 400

    try:
        session = get_authenticated_session()
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.session = session

        raw = zp.get_team_riders(club_id) or {}
        rows = raw.get("data") or []

        simplified = []
        for row in rows:
            if not isinstance(row, dict):
                continue

            zwid = row.get("zwid")
            if zwid is None:
                continue

            name = row.get("name")
            if isinstance(name, str):
                name = name.strip()
            else:
                name = None

            rank_raw = row.get("rank")
            rank_num = None

            if isinstance(rank_raw, (int, float)):
                rank_num = float(rank_raw)
            elif isinstance(rank_raw, str):
                s = rank_raw.strip().replace(",", ".")
                if s:
                    try:
                        rank_num = float(s)
                    except Exception:
                        rank_num = None

            simplified.append(
                {
                    "zwid": zwid,
                    "name": name,
                    "rank": rank_num,
                    "rankRaw": rank_raw,
                }
            )

        result = overwrite_zwiftpower_club_members_in_firestore(simplified)
        return jsonify(
            {
                "status": "success",
                "clubId": club_id,
                "fetched": len(rows),
                "stored": result["memberCount"],
                "deleted": result["deleted"],
                "upserted": result["upserted"],
                "syncedAt": result["syncedAt"],
            }
        )

    except Exception as e:
        print(f"Error refreshing ZwiftPower club roster: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

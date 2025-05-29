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

CONTENT_API_KEY = os.getenv("CONTENT_API_KEY", "your_content_api_key")

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
        results = zp.get_team_results(club_id)
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
        upgrade_data = firebase.compare_rider_categories(today, yesterday)

        if not upgrade_data.get("upgradedZPCategory") and not upgrade_data.get("upgradedZwiftRacingCategory") and not upgrade_data.get("upgradedZRSCategory"):
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

@app.route('/discord_users', methods=['GET'])
def get_discord_users():
    """Retrieve all discord users from Firebase"""
    try:
        # Get limit parameter from query string, default to 100
        limit = request.args.get('limit', default=100, type=int)
        
        # Call the get_collection function from firebase module
        users = firebase.get_collection('discord_users', limit=limit)
        
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
            
            return render_template(
                'discord_members.html',
                members=members,
                zwift_riders=zwift_riders,
                linked_count=linked_count,
                unlinked_count=unlinked_count
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
            scheduled_messages = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
            
            return jsonify({
                "welcome_messages": welcome_messages,
                "scheduled_messages": scheduled_messages,
                "stats": {
                    "total_welcome": len(welcome_messages),
                    "total_scheduled": len(scheduled_messages),
                    "active_welcome": len([m for m in welcome_messages if m.get('active', False)]),
                    "active_scheduled": len([m for m in scheduled_messages if m.get('active', False)])
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
            scheduled_messages = firebase.get_collection('scheduled_messages', limit=100, include_id=True)
            
            content_stats = {
                'total_welcome': len(welcome_messages),
                'total_scheduled': len(scheduled_messages),
                'active_welcome': len([m for m in welcome_messages if m.get('active', False)]),
                'active_scheduled': len([m for m in scheduled_messages if m.get('active', False)])
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
        
        # Group messages by channel and daily_probability
        channel_groups = {}
        for message in eligible_messages:
            channel_id = message.get('channel_id')
            if channel_id not in channel_groups:
                channel_groups[channel_id] = {
                    'daily_probability': message.get('schedule', {}).get('daily_probability', 0.1),
                    'messages': []
                }
            channel_groups[channel_id]['messages'].append(message)
        
        messages_to_send = []
        
        # For each channel, check if we should send a message today
        for channel_id, group in channel_groups.items():
            daily_probability = group['daily_probability']
            
            # Roll the dice for this channel
            if random.random() < daily_probability:
                print(f"[DEBUG] Channel {channel_id} won the probability roll (p={daily_probability})")
                
                # Select a message based on likelihood weights
                messages = group['messages']
                weights = []
                for msg in messages:
                    likelihood = msg.get('schedule', {}).get('likelihood', 1.0)
                    weights.append(likelihood)
                
                # Weighted random selection
                if weights and sum(weights) > 0:
                    selected_message = random.choices(messages, weights=weights)[0]
                    messages_to_send.append(selected_message)
                    print(f"[DEBUG] Selected message: {selected_message.get('title', 'Unknown')} (likelihood={selected_message.get('schedule', {}).get('likelihood', 1.0)})")
                else:
                    # Fallback to random selection if no weights
                    selected_message = random.choice(messages)
                    messages_to_send.append(selected_message)
                    print(f"[DEBUG] Selected message (no weights): {selected_message.get('title', 'Unknown')}")
            else:
                print(f"[DEBUG] Channel {channel_id} did not win the probability roll (p={daily_probability})")
        
        return jsonify({
            "messages_to_send": messages_to_send,
            "total_eligible": len(eligible_messages),
            "channels_checked": len(channel_groups)
        })
        
    except Exception as e:
        print(f"Error in check_probability_and_select: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

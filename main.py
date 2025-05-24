import os
import time
import inspect
import logging
from flask import Flask, request, jsonify, render_template
from zwiftpower import ZwiftPower
from zwiftcommentator import ZwiftCommentator
import requests
from datetime import datetime, timedelta
import firebase
from discord_api import DiscordAPI
from zwift import ZwiftAPI

app = Flask(__name__)

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
def get_discord_members():
    """Get all Discord members with their information"""
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

@app.route('/', methods=['GET'])
def index():
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

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

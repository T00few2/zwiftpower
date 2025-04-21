import os
import time
from flask import Flask, request, jsonify, render_template
from zwiftpower import ZwiftPower
from zwiftcommentator import ZwiftCommentator
import requests
from datetime import datetime, timedelta
import firebase
from discord_api import DiscordAPI
from zwift import ZwiftAPI

app = Flask(__name__)

# Global variable to cache an authenticated session.
cached_session = None
cached_session_timestamp = None 
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

@app.route('/rider_zrs', methods=['GET'])
def rider_zrs_bulk():
    """
    Expects a query parameter "rider_ids" containing comma-separated rider IDs.
    For example: /rider_zrs?rider_ids=15690,15691,15692
    Returns a JSON array with each rider's racing score.
    """
    rider_ids_str = request.args.get("rider_ids")
    if not rider_ids_str:
        return jsonify({"error": "Missing rider_ids query parameter"}), 400

    try:
        rider_ids = [int(r.strip()) for r in rider_ids_str.split(",") if r.strip()]
    except ValueError:
        return jsonify({"error": "Invalid rider_ids format; must be comma-separated integers"}), 400

    results = []
    try:
        # Get the cached authenticated session.
        session = get_authenticated_session()
        # Create a ZwiftPower instance using the authenticated session.
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.session = session
        delay = 10  # seconds between requests
        for rid in rider_ids:
            zrs = zp.get_rider_zrs(rid)
            if zrs:
                results.append({"rider_id": rid, "zrs": zrs})
            else:
                results.append({"rider_id": rid, "error": "Racing Score not found"})
            time.sleep(delay)  # Respect the crawl-delay
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/team_riders/<int:club_id>', methods=['GET'])
def team_riders(club_id: int):
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

@app.route('/rider_data/<int:rider_id>', methods=['GET'])
def rider_data(rider_id: int):
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
   
    try:
        print("[DEBUG] Starting upgrade comment generation...")

        today = datetime.now().strftime("%y%m%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%y%m%d")


        if not today or not yesterday:
            return jsonify({"error": "Missing 'today' or 'yesterday' in request body"}), 400

        # Build the compare endpoint URL
        compare_url = f"https://www.dzrracingseries.com/api/zr/compare?today={today}&yesterday={yesterday}"

        # Fetch the upgrade data
        print(f"[DEBUG] Fetching upgrade data from {compare_url}")
        response = requests.get(compare_url)
        response.raise_for_status()
        upgrade_data = response.json()

        if not upgrade_data.get("upgradedZPCategory") and not upgrade_data.get("upgradedZwiftRacingCategory"):
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

@app.route('/api/enrich_club_stats', methods=['POST'])
def enrich_club_stats():
    """Enrich club_stats with racing scores from Zwift profiles and update in Firebase"""
    try:
        # Get the latest club_stats from Firebase
        club_stats = firebase.get_latest_document("club_stats")
        
        if not club_stats or len(club_stats) == 0:
            return jsonify({"status": "error", "message": "No club_stats data found"}), 404
            
        # Initialize Zwift API client
        zwift_api = ZwiftAPI(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zwift_api.authenticate()
        
        # Track stats
        stats = club_stats[0]
        total_riders = len(stats["data"]["riders"])
        processed_riders = 0
        riders_with_scores = 0
        
        print(f"Starting to enrich {total_riders} riders with racing scores")
        
        # Process each rider
        for rider in stats["data"]["riders"]:
            if "riderId" not in rider:
                continue
                
            rider_id = rider["riderId"]
            rider_name = rider.get("name", "Unknown")
            processed_riders += 1
            
            print(f"Processing rider {processed_riders}/{total_riders}: {rider_name} (ID: {rider_id})")
            
            # Get the rider's profile from Zwift API
            profile = zwift_api.get_profile(rider_id)
            
            # Add racing score if available
            if profile and "competitionMetrics" in profile and "racingScore" in profile["competitionMetrics"]:
                racing_score = profile["competitionMetrics"]["racingScore"]
                rider["racingScore"] = racing_score
                riders_with_scores += 1
                print(f"Added racing score {racing_score} to rider {rider_name}")
            else:
                print(f"Could not get racing score for rider {rider_name}")
                
            # Add a small delay to avoid rate limiting
            time.sleep(10)
        
        print(f"Enrichment complete. Added racing scores to {riders_with_scores}/{total_riders} riders")
        
        # Update the club_stats document in Firebase
        # Use the timestamp from the original document
        timestamp = stats.get("timestamp", datetime.now().isoformat())
        club_id = stats.get("clubId", "unknown")
        
        # Create a new document to replace the old one
        updated_doc = {
            "clubId": club_id,
            "timestamp": timestamp,
            "data": stats["data"]
        }
        
        # If there was an expiresAt field, keep it
        if "expiresAt" in stats:
            updated_doc["expiresAt"] = stats["expiresAt"]
        
        # Add the updated document to club_stats collection
        db_ref = firebase.db.collection("club_stats").document()
        db_ref.set(updated_doc)
        
        return jsonify({
            "status": "success", 
            "message": f"Updated club_stats with racing scores for {riders_with_scores} riders",
            "processed": processed_riders,
            "with_scores": riders_with_scores,
            "total": total_riders
        })
        
    except Exception as e:
        print(f"Error enriching club_stats: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

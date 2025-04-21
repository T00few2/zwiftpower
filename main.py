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
        # Get parameters
        batch_size = request.json.get('batch_size', 5)  # Process 5 riders per call by default
        start_index = request.json.get('start_index', 0)  # Start from the beginning by default
        
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
        
        # Calculate end index (don't go beyond total riders)
        end_index = min(start_index + batch_size, total_riders)
        
        print(f"Processing batch of riders {start_index+1} to {end_index} of {total_riders}")
        
        # Process only the specified batch of riders
        for i in range(start_index, end_index):
            rider = stats["data"]["riders"][i]
            if "riderId" not in rider:
                continue
                
            rider_id = rider["riderId"]
            rider_name = rider.get("name", "Unknown")
            processed_riders += 1
            
            print(f"Processing rider {i+1}/{total_riders}: {rider_name} (ID: {rider_id})")
            
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
                
            # Add a delay to avoid rate limiting
            time.sleep(3)  # Reduced delay to fit within function timeouts
        
        print(f"Batch complete. Added racing scores to {riders_with_scores}/{processed_riders} riders")
        
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
        
        # Calculate if we have more riders to process
        next_index = end_index
        has_more = next_index < total_riders
        
        return jsonify({
            "status": "success", 
            "message": f"Updated club_stats with racing scores for {riders_with_scores} riders",
            "processed": processed_riders,
            "with_scores": riders_with_scores,
            "total_riders": total_riders,
            "current_batch": {
                "start": start_index,
                "end": end_index,
            },
            "has_more": has_more,
            "next_index": next_index if has_more else None
        })
        
    except Exception as e:
        print(f"Error enriching club_stats: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/enrich_all_riders', methods=['POST'])
def start_enrich_process():
    """Start the enrichment process for all riders by calling the batch API endpoint"""
    try:
        # Get the latest club_stats to determine how many riders to process
        club_stats = firebase.get_latest_document("club_stats")
        
        if not club_stats or len(club_stats) == 0:
            return jsonify({"status": "error", "message": "No club_stats data found"}), 404
            
        # Count riders
        total_riders = len(club_stats[0]["data"]["riders"])
        batch_size = request.json.get('batch_size', 5)
        
        # Calculate number of batches needed
        batches_needed = (total_riders + batch_size - 1) // batch_size  # Ceiling division
        
        # Create a Cloud Task or return instructions for client-side processing
        return jsonify({
            "status": "success",
            "message": f"To process all {total_riders} riders, make {batches_needed} separate API calls",
            "total_riders": total_riders,
            "batch_size": batch_size,
            "batches_needed": batches_needed,
            "instructions": "Call /api/enrich_club_stats with 'start_index' parameter, incrementing by batch_size each time"
        })
        
    except Exception as e:
        print(f"Error starting enrichment process: {str(e)}")
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
        
        # Clear existing queue
        queue_collection = firebase.db.collection("rider_score_queue")
        existing_docs = queue_collection.limit(500).stream()
        for doc in existing_docs:
            doc.reference.delete()
        
        # Add riders without racing scores to queue
        queued_count = 0
        for rider in riders:
            if "riderId" not in rider:
                continue
                
            # Only queue riders without racing scores
            if "racingScore" not in rider:
                rider_id = rider["riderId"]
                rider_name = rider.get("name", "Unknown")
                
                # Add to queue with status "pending"
                queue_collection.add({
                    "riderId": rider_id,
                    "name": rider_name,
                    "status": "pending",
                    "addedAt": datetime.now(),
                    "processedAt": None,
                    "racingScore": None
                })
                queued_count += 1
        
        return jsonify({
            "status": "success",
            "message": f"Initialized queue with {queued_count} riders",
            "total_riders": len(riders),
            "queued_riders": queued_count
        })
        
    except Exception as e:
        print(f"Error initializing rider queue: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/process_rider_queue', methods=['POST'])
def process_rider_queue():
    """Process a batch of riders from the queue"""
    try:
        # Get parameters
        batch_size = request.json.get('batch_size', 3) if request.json else 3  # Process 3 riders per call by default
        
        # Get pending riders from queue
        queue_ref = firebase.db.collection("rider_score_queue")
        pending_riders_query = queue_ref.where("status", "==", "pending").limit(batch_size)
        pending_docs = list(pending_riders_query.stream())
        
        if not pending_docs:
            # No more pending riders, check if we should update club_stats
            completed_riders = queue_ref.where("status", "==", "completed").stream()
            completed_list = list(completed_riders)
            
            if completed_list:
                # Update the main club_stats with all processed scores
                return update_club_stats_from_queue()
            else:
                return jsonify({
                    "status": "success",
                    "message": "No riders in queue to process"
                })
        
        # Initialize Zwift API client
        zwift_api = ZwiftAPI(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zwift_api.authenticate()
        
        processed_count = 0
        success_count = 0
        
        # Process each rider
        for doc in pending_docs:
            doc_id = doc.id  # Get the document ID directly
            rider_data = doc.to_dict()
            
            rider_id = rider_data.get("riderId")
            rider_name = rider_data.get("name", "Unknown")
            
            if not rider_id:
                continue
                
            processed_count += 1
            print(f"Processing rider {rider_name} (ID: {rider_id})")
            
            try:
                # Get the rider's profile
                profile = zwift_api.get_profile(rider_id)
                
                # Update the queue document
                queue_item_ref = queue_ref.document(doc_id)
                
                # Add racing score if available
                if profile and "competitionMetrics" in profile and "racingScore" in profile["competitionMetrics"]:
                    racing_score = profile["competitionMetrics"]["racingScore"]
                    
                    queue_item_ref.update({
                        "status": "completed",
                        "processedAt": datetime.now(),
                        "racingScore": racing_score
                    })
                    
                    success_count += 1
                    print(f"Added racing score {racing_score} to rider {rider_name}")
                else:
                    queue_item_ref.update({
                        "status": "failed",
                        "processedAt": datetime.now(),
                        "error": "Racing score not found in profile"
                    })
                    print(f"Could not find racing score for rider {rider_name}")
            except Exception as rider_error:
                # Update status to failed
                queue_ref.document(doc_id).update({
                    "status": "failed",
                    "processedAt": datetime.now(),
                    "error": str(rider_error)
                })
                print(f"Error processing rider {rider_name}: {str(rider_error)}")
            
            # Add delay between riders
            time.sleep(3)
        
        # Get queue statistics
        pending_count = len(list(queue_ref.where("status", "==", "pending").stream()))
        completed_count = len(list(queue_ref.where("status", "==", "completed").stream()))
        failed_count = len(list(queue_ref.where("status", "==", "failed").stream()))
        
        return jsonify({
            "status": "success",
            "message": f"Processed {processed_count} riders, {success_count} successful",
            "stats": {
                "processed_this_batch": processed_count,
                "successful_this_batch": success_count,
                "pending": pending_count,
                "completed": completed_count,
                "failed": failed_count
            },
            "queue_empty": pending_count == 0
        })
        
    except Exception as e:
        print(f"Error processing rider queue: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

def update_club_stats_from_queue():
    """Update club_stats with all processed racing scores from the queue"""
    try:
        # Get the latest club_stats
        club_stats = firebase.get_latest_document("club_stats")
        
        if not club_stats or len(club_stats) == 0:
            return jsonify({"status": "error", "message": "No club_stats data found"}), 404
            
        # Get completed riders from queue
        queue_ref = firebase.db.collection("rider_score_queue")
        completed_riders = queue_ref.where("status", "==", "completed").stream()
        completed_dict = {str(doc.to_dict()["riderId"]): doc.to_dict()["racingScore"] for doc in completed_riders}
        
        if not completed_dict:
            return jsonify({
                "status": "success",
                "message": "No completed riders in queue to update club_stats with"
            })
        
        # Update the club_stats with all processed scores
        stats = club_stats[0]
        updated_count = 0
        
        for rider in stats["data"]["riders"]:
            if "riderId" not in rider:
                continue
                
            rider_id_str = str(rider["riderId"])
            if rider_id_str in completed_dict:
                rider["racingScore"] = completed_dict[rider_id_str]
                updated_count += 1
        
        # Create a new document with updated data
        timestamp = stats.get("timestamp", datetime.now().isoformat())
        club_id = stats.get("clubId", "unknown")
        
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
        
        # Clear the queue if requested
        should_clear_queue = request.json.get('clear_queue', True)
        if should_clear_queue:
            batch = firebase.db.batch()
            docs = queue_ref.limit(500).stream()
            for doc in docs:
                batch.delete(doc.reference)
            batch.commit()
        
        return jsonify({
            "status": "success",
            "message": f"Updated club_stats with {updated_count} racing scores from queue",
            "queue_cleared": should_clear_queue
        })
        
    except Exception as e:
        print(f"Error updating club_stats from queue: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/queue_status', methods=['GET'])
def get_queue_status():
    """Get the current status of the rider queue"""
    try:
        queue_ref = firebase.db.collection("rider_score_queue")
        
        # Get counts for each status
        pending_count = len(list(queue_ref.where("status", "==", "pending").stream()))
        completed_count = len(list(queue_ref.where("status", "==", "completed").stream()))
        failed_count = len(list(queue_ref.where("status", "==", "failed").stream()))
        total_count = pending_count + completed_count + failed_count
        
        # Get a sample of recent items
        recent_items = [doc.to_dict() for doc in queue_ref.order_by("processedAt", direction=firebase.firestore.Query.DESCENDING).limit(5).stream()]
        
        return jsonify({
            "status": "success",
            "queue_stats": {
                "total": total_count,
                "pending": pending_count,
                "completed": completed_count,
                "failed": failed_count,
                "percent_complete": (completed_count / total_count * 100) if total_count > 0 else 0
            },
            "recent_items": recent_items,
            "queue_empty": pending_count == 0
        })
        
    except Exception as e:
        print(f"Error getting queue status: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

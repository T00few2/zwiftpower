import os
import time
from flask import Flask, request, jsonify
from zwiftpower import ZwiftPower  # your ZwiftPower class
from zwiftcommentator import ZwiftCommentator  # your ZwiftCommentator class
import requests

app = Flask(__name__)

# Global variable to cache an authenticated session.
cached_session = None
cached_session_timestamp = None  # Optionally, track when we logged in
SESSION_VALIDITY = 3600  # seconds (adjust based on how long the session is expected to be valid)

# Get credentials from environment variables.
ZWIFT_USERNAME = os.getenv("ZWIFT_USERNAME", "your_username")
ZWIFT_PASSWORD = os.getenv("ZWIFT_PASSWORD", "your_password")

OPENAI_KEY = os.getenv("OPENAI_KEY", "your_openai_key")

DISCORD_GOSSIP_ID = os.getenv("DISCORD_GOSSIP_ID", "your_discord_gossip_id")
DISCORD_BOT_URL = os.getenv("DISCORD_BOT_URL", "your_discord_bot_url")

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
        delay = 10  # seconds between requests (as required by robots.txt)
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


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

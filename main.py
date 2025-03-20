import os
import time
from flask import Flask, request, jsonify
from zwiftpower import ZwiftPower  # assume your class is in zwiftpower.py

app = Flask(__name__)

# Get credentials from environment variables:
ZWIFT_USERNAME = os.getenv("ZWIFT_USERNAME", "your_username")
ZWIFT_PASSWORD = os.getenv("ZWIFT_PASSWORD", "your_password")

@app.route('/team_riders/<int:club_id>', methods=['GET'])
def team_riders(club_id: int):
    try:
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.login()
        data = zp.get_team_riders(club_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/rider_zrs', methods=['GET'])
def rider_zrs_bulk():
    """
    Expects a query parameter "rider_ids" containing comma-separated IDs.
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
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.login()
        delay = 30  # delay in seconds between requests
        for rid in rider_ids:
            zrs = zp.get_rider_zrs(rid)
            if zrs:
                results.append({"rider_id": rid, "zrs": zrs})
            else:
                results.append({"rider_id": rid, "error": "Racing Score not found"})
            time.sleep(delay)  # pause before processing the next rider_id
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

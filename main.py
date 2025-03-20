import os
from flask import Flask, request, jsonify
from zwiftpower import ZwiftPower  # assume your class is in zwiftpower.py

app = Flask(__name__)

# Optionally, get credentials from environment variables:
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

@app.route('/rider_zrs/<int:rider_id>', methods=['GET'])
def rider_zrs(rider_id: int):
    try:
        zp = ZwiftPower(ZWIFT_USERNAME, ZWIFT_PASSWORD)
        zp.login()
        zrs = zp.get_rider_zrs(rider_id)
        if not zrs:
            return jsonify({"error": "Racing Score not found"}), 404
        return jsonify({"rider_id": rider_id, "zrs": zrs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Cloud Run expects the port to be specified via the PORT environment variable.
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

import os
import psycopg2
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://logs:wrGNCPJfbsm7@localhost:5432/logs')

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


@app.route('/health')
def health():
    return jsonify({"status": "healthy", "service": "log_server"})


@app.route('/api/visits', methods=['POST'])
@app.route('/log', methods=['POST'])
@app.route('/log/public', methods=['POST'])
def log_visit():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid format"}), 400

    user_id = data.get('anon_id') or data.get('id') or 'anonymous'
    url = data.get('url', '')
    date = data.get('timestamp') or data.get('date') or datetime.utcnow().isoformat()

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO log (user_id, ip, url, date, processed, encrypted_data) VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, request.remote_addr or '', url, date, True, False)
        )
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"message": "OK"}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=False)

import os
import re
import json
import sqlite3
import sqlite_vec
from flask import Flask, request, jsonify
from sentence_transformers import SentenceTransformer

app = Flask(__name__)

MODEL_NAME = os.getenv('MODEL_NAME', 'all-MiniLM-L6-v2')
DB_PATH = os.getenv('DB_PATH', '/data/q.sqlite')

model = SentenceTransformer(MODEL_NAME)

_sqlite_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_sqlite_conn.enable_load_extension(True)
sqlite_vec.load(_sqlite_conn)


def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'<.*?>', '', text)
    text = re.sub(r'&[a-z0-9]+;', ' ', text)
    text = re.sub(r'["&<>]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


VEC_TOP_K = 20
BM25_TOP_K = 30
RRF_CONST = 60


def check_fts():
    cursor = _sqlite_conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='questions_fts'")
    return cursor.fetchone() is not None


_HAS_FTS = None


def has_fts():
    global _HAS_FTS
    if _HAS_FTS is None:
        _HAS_FTS = check_fts()
    return _HAS_FTS


def bm25_search(text, limit=BM25_TOP_K):
    words = re.findall(r'\w+', text.lower())
    if not words:
        return {}
    seen = set()
    unique = [w for w in words if w not in seen and not seen.add(w) and len(w) > 1]
    if not unique:
        return {}
    query = ' OR '.join(unique[:30])
    try:
        cursor = _sqlite_conn.cursor()
        rows = cursor.execute(
            "SELECT rowid FROM questions_fts WHERE questions_fts MATCH ? ORDER BY rank LIMIT ?",
            [query, limit]
        ).fetchall()
        return {r[0]: i + 1 for i, r in enumerate(rows)}
    except Exception:
        return {}


@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


@app.route('/health')
def health():
    return jsonify({"status": "healthy", "service": "vector_service", "fts": has_fts()})


@app.route('/similarity', methods=['POST'])
def similarity():
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({"error": "Missing 'text' field"}), 400

    text = clean_text(data['text'])[:1000]
    print(f"=== CLEANED TEXT ===\n{text}\n=== END ===", flush=True)
    if not text:
        return jsonify({"error": "Text is empty"}), 400

    embedding = model.encode(text).tolist()
    embedding_json = json.dumps(embedding)

    cursor = _sqlite_conn.cursor()
    vec_rows = cursor.execute(
        """
        SELECT q.id, q.title, q.link, e.distance
        FROM (
            SELECT question_id, distance
            FROM question_embeddings
            WHERE embedding MATCH ?
              AND k = ?
        ) e
        JOIN questions q ON q.id = e.question_id
        ORDER BY e.distance
        """,
        [embedding_json, VEC_TOP_K]
    ).fetchall()

    vec_by_id = {}
    for i, row in enumerate(vec_rows):
        vec_by_id[row[0]] = {
            "title": row[1],
            "link": row[2],
            "distance": round(row[3], 6),
            "vec_rank": i + 1
        }

    bm25_by_id = bm25_search(text) if has_fts() else {}

    if not bm25_by_id:
        # Fallback: pure vector top-3
        top3 = list(vec_by_id.values())[:3]
        return jsonify(top3)

    # RRF hybrid
    all_ids = set(vec_by_id.keys()) | set(bm25_by_id.keys())
    scored = []
    for qid in all_ids:
        info = vec_by_id.get(qid)
        if not info:
            continue
        vr = info["vec_rank"]
        br = bm25_by_id.get(qid, 9999)
        rrf = 0.7 * (1.0 / (RRF_CONST + vr)) + 0.3 * (1.0 / (RRF_CONST + br))
        scored.append((rrf, info))

    scored.sort(key=lambda x: -x[0])
    results = [info for _, info in scored[:3]]

    return jsonify(results)


@app.route('/recommend', methods=['POST'])
def recommend():
    data = request.get_json()
    if not data or 'texts' not in data or not isinstance(data['texts'], list) or len(data['texts']) == 0:
        return jsonify({"error": "Missing 'texts' array"}), 400

    texts = [clean_text(t)[:1000] for t in data['texts'] if isinstance(t, str) and t.strip()]
    if not texts:
        return jsonify({"error": "No valid texts"}), 400

    embeddings = [model.encode(t) for t in texts]
    avg_embedding = sum(embeddings) / len(embeddings)
    embedding_json = json.dumps(avg_embedding.tolist())

    cursor = _sqlite_conn.cursor()
    rows = cursor.execute(
        """
        SELECT q.id, q.title, q.link, e.distance
        FROM (
            SELECT question_id, distance
            FROM question_embeddings
            WHERE embedding MATCH ?
              AND k = 3
        ) e
        JOIN questions q ON q.id = e.question_id
        ORDER BY e.distance
        """,
        [embedding_json]
    ).fetchall()

    results = [
        {"question_id": row[0], "title": row[1], "link": row[2], "distance": round(row[3], 6)}
        for row in rows
    ]

    return jsonify(results)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5006, debug=False)

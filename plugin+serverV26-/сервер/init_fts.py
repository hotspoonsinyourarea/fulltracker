import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "/opt/vector_service/q.sqlite"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Check if FTS5 index already exists
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='questions_fts'")
if cur.fetchone():
    print("FTS5 index already exists, skipping.")
else:
    print("Creating FTS5 index on questions.title...")
    cur.execute("CREATE VIRTUAL TABLE questions_fts USING fts5(title)")
    cur.execute("SELECT COUNT(*) FROM questions")
    count = cur.fetchone()[0]
    cur.execute("INSERT INTO questions_fts(rowid, title) SELECT id, title FROM questions")
    conn.commit()
    print(f"FTS5 index created and populated with {count} rows.")

conn.close()
print("Done.")

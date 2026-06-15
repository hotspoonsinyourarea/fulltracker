#!/usr/bin/env python3
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
import json
import time

# Простой ограничитель частоты: минимальный интервал между API-запросами
class SimpleRateLimiter:
    def __init__(self, rate_per_sec: float):
        self._interval = 1.0 / rate_per_sec
        self._last = 0.0
    def wait(self):
        now = time.time()
        if self._last != 0:
            elapsed = now - self._last
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
                now = time.time()
        self._last = now

# Глобальный ограничитель (например, 4 запроса в секунду)
RATE_LIMITER = SimpleRateLimiter(4.0)

# Минимальный загрузчик: получает вопросы с тегом 'python' из Stack Exchange API
# и сохраняет их в SQLite БД.

DB_PATH = "stackoverflow_questions.db"
API_BASE = "https://api.stackexchange.com/2.3/questions"
API_KEY = "rl_VjbUR697iCo58S9footpYDyuN"

def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    return sqlite3.connect(db_path)

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY,
        title TEXT,
        link TEXT,
        description TEXT,
        answers INTEGER,
        votes INTEGER,
        views INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fetch_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_page INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("INSERT OR IGNORE INTO fetch_state (id, last_page) VALUES (1, 0)")
    conn.commit()

def insert_question(conn: sqlite3.Connection, q: dict) -> int:
    sql = (
        "INSERT OR IGNORE INTO questions (id, title, link, description, answers, votes, views) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    cur = conn.execute(sql, (
        q["id"],
        q["title"],
        q["link"],
        q["description"],
        int(q["answers"]),
        int(q["votes"]),
        int(q["views"]),
    ))
    conn.commit()
    return cur.rowcount

def get_last_page(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT last_page FROM fetch_state WHERE id = 1")
    return cur.fetchone()[0]

def save_last_page(conn: sqlite3.Connection, page: int) -> None:
    conn.execute("UPDATE fetch_state SET last_page = ? WHERE id = 1", (page,))
    conn.commit()

def fetch_questions(conn: sqlite3.Connection, start_page: int, tag: str = "python", pagesize: int = 100, max_pages: int = 2) -> list:
    page = start_page
    results = []
    while page <= max_pages:
        RATE_LIMITER.wait()
        params = {
            "order": "desc",
            "sort": "votes",
            "tagged": tag,
            "site": "stackoverflow",
            "pagesize": pagesize,
            "page": page,
            "key": API_KEY,
            "filter": "withbody",
        }
        url = API_BASE + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "StackParser/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"HTTP ошибка {e.code}: {e.read().decode()}")
            break
        except urllib.error.URLError as e:
            print(f"URL ошибка: {e.reason}")
            break
        quota_remaining = data.get("quota_remaining", "?")
        print(f"Страница {page}: запросов осталось={quota_remaining}")
        if "error_id" in data:
            print(f"API ошибка {data['error_id']}: {data.get('error_message', '')}")
            break
        if "backoff" in data:
            backoff = int(data["backoff"])
            time.sleep(backoff)
        items = data.get("items", [])
        for it in items:
            results.append({
                "id": it.get("question_id"),
                "title": it.get("title"),
                "link": it.get("link"),
                "description": it.get("body", ""),
                "answers": it.get("answer_count", 0),
                "votes": it.get("score", 0),
                "views": it.get("view_count", 0),
            })
        save_last_page(conn, page)
        if not data.get("has_more", False):
            break
        page += 1
    return results

def main() -> None:
    conn = connect()
    init_db(conn)
    try:
        last_page = get_last_page(conn)
        start_page = last_page + 1
        print(f"Продолжаем со страницы {start_page}.")
        questions = fetch_questions(conn, start_page, tag="python", pagesize=100, max_pages=1000)
        print(f"Загружено {len(questions)} вопросов по тегу 'python'.")
        inserted = 0
        for q in questions:
            inserted += insert_question(conn, q)
        print(f"Добавлено {inserted} новых вопросов в БД.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()

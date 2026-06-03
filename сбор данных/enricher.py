# enricher.py
# Модуль второго этапа ETL-конвейера: многопоточная загрузка ответов
# и комментариев через Stack Exchange API v2.3 с ограничителем частоты
# запросов (Token Bucket) и выделенным потоком записи в базу данных.

import sqlite3
import requests
import time
import html
import logging
import threading
import queue
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# Настройка логирования с отметкой времени и уровнем
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ── Ограничитель частоты запросов (алгоритм Token Bucket) ─────────────────

class RateLimiter:
    """
    Потокобезопасный ограничитель частоты запросов на основе алгоритма
    Token Bucket («токен-ведро»).
    Принцип: ведро наполняется токенами со скоростью max_per_second шт/сек
    до максимальной ёмкости max_per_second. Каждый запрос «расходует» один
    токен. Если токенов недостаточно, поток засыпает до их накопления.
    """
    def __init__(self, max_per_second):
        """
        :param max_per_second: максимальное число запросов в секунду
        """
        self.max_per_second = max_per_second
        self.tokens = float(max_per_second)  # начальный уровень --- полное ведро
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        """
        Блокирует вызывающий поток до тех пор, пока в ведре не накопится
        хотя бы один токен, затем списывает один токен.
        """
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.max_per_second,
                                  self.tokens + (now - self.last_refill) * self.max_per_second)
                self.last_refill = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.max_per_second
            time.sleep(wait)

# ── Поток записи в базу данных (паттерн «производитель-потребитель») ─────────

class DBWriter(threading.Thread):
    """
    Демонический поток, принимающий SQL-задания через потокобезопасную очередь
    и сбрасывающий их в базу данных пакетами.
    Архитектурная роль: рабочие потоки обогащения никогда не обращаются
    к SQLite напрямую --- только через DBWriter.submit(). Это исключает
    конфликты блокировок при конкурентной записи.
    """
    def __init__(self, db_path, batch_size=50, flush_interval=0.2):
        """
        :param db_path: путь к файлу базы данных SQLite
        :param batch_size: число заданий, при достижении которого
                           выполняется принудительный сброс буфера
        :param flush_interval: максимальный интервал между сбросами (секунды)
        """
        super().__init__(daemon=True)
        self.db_path = db_path
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.error_count = 0

    def run(self):
        """
        Основной цикл потока: извлекает задания из очереди, накапливает их
        в буфере и периодически фиксирует в базе данных единой транзакцией.
        При трёх последовательных ошибках выполняется переподключение к БД.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        pending = []
        last_flush = time.monotonic()

        def reconnect():
            """Переподключение к базе данных при сбое соединения."""
            nonlocal conn, cursor
            try:
                conn.close()
            except Exception:
                pass
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            logging.info("DBWriter: переподключение к БД выполнено")

        # Основной цикл: работает, пока не установлен stop_event
        # и очередь не опустела
        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                # Ожидаем следующее задание с таймаутом flush_interval
                item = self.queue.get(timeout=self.flush_interval)
                if item is None:
                    # Сигнал завершения работы
                    self.queue.task_done()
                    break
                pending.append(item)
                self.queue.task_done()
            except queue.Empty:
                pass

            now = time.monotonic()
            # Сбрасываем буфер при достижении порога или истечении интервала
            should_flush = (
                len(pending) >= self.batch_size or
                (pending and (now - last_flush) >= self.flush_interval)
            )
            if should_flush:
                try:
                    for sql, params in pending:
                        cursor.execute(sql, params)
                    conn.commit()
                    pending.clear()
                    last_flush = time.monotonic()
                    self.error_count = 0
                except Exception as e:
                    self.error_count += 1
                    logging.error(f"DBWriter: ошибка записи ({self.error_count}): {e}")
                    if self.error_count >= 6:
                        pending.clear()
                        self.error_count = 0
                        logging.warning("DBWriter: буфер очищен после 6 ошибок подряд")
                    elif self.error_count >= 3:
                        reconnect()
                    time.sleep(0.5)

        # Финальный сброс оставшихся заданий перед завершением потока
        if pending:
            try:
                for sql, params in pending:
                    cursor.execute(sql, params)
                conn.commit()
                logging.info(f"DBWriter: финальный сброс {len(pending)} заданий")
            except Exception as e:
                logging.error(f"DBWriter: ошибка финальной записи: {e}")
        conn.close()
        logging.info("DBWriter: соединение с БД закрыто")

    def submit(self, sql, params):
        """
        Добавляет SQL-задание в очередь на запись.
        Потокобезопасен: может вызываться из любого рабочего потока.
        :param sql: строка SQL-запроса с плейсхолдерами '?'
        :param params: кортеж параметров для подстановки
        """
        self.queue.put((sql, params))

    def stop(self):
        """
        Инициирует корректное завершение потока:
        устанавливает флаг остановки, помещает в очередь сигнал None
        и ожидает завершения потока (join).
        """
        self.stop_event.set()
        self.queue.put(None)
        self.join()

# ── Основной класс обогащения данных ─────────────────────────────────────────

class StackOverflowEnricher:
    """
    Класс многопоточного обогащения базы данных вопросов Stack Overflow:
    для каждого необработанного вопроса загружает ответы и комментарии
    через Stack Exchange API v2.3 и сохраняет их через DBWriter.

    Ключевые механизмы устойчивости:
      - RateLimiter: соблюдение квоты 30 запросов/сек
      - HTTPAdapter с Retry: автоповтор при 5xx-ошибках
      - Флаг enriched: идемпотентность при повторных запусках
      - DBWriter: бесконфликтная конкурентная запись
    """
    def __init__(self, db_path, api_key=None, site='stackoverflow'):
        """
        Инициализирует компоненты: RateLimiter, HTTP-сессию с адаптером
        повторных попыток, DBWriter, и приводит схему БД к целевому виду.
        :param db_path: путь к файлу базы данных SQLite
        :param api_key: ключ Stack Exchange API (увеличивает квоту до 10 000/сут)
        :param site: идентификатор сайта ('stackoverflow' или 'ru.stackoverflow')
        """
        self.db_path = db_path
        self.api_key = api_key
        self.site = site
        self.base_url = "https://api.stackexchange.com/2.3/"
        self.print_lock = threading.Lock()

        # Инициализация ограничителя частоты запросов
        self.rate_limiter = RateLimiter(max_per_second=30)

        # Настройка HTTP-сессии с экспоненциальной стратегией повторных попыток
        self.session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

        # Запуск потока-писателя в базу данных
        self.writer = DBWriter(db_path)
        self.writer.start()

        # Создание таблиц и индексов при необходимости
        self._setup_database()

    def safe_print(self, msg):
        """
        Потокобезопасный вывод сообщения через logging.
        Использует print_lock для исключения перемешивания вывода из разных потоков.
        :param msg: строка для вывода
        """
        with self.print_lock:
            logging.info(msg)

    def _setup_database(self):
        """
        Создаёт таблицы answers и comments (если не существуют),
        добавляет колонку enriched в таблицу questions (если отсутствует),
        а также создаёт индексы для ускорения аналитических запросов.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # Таблица ответов с уникальным ограничением на answer_id
        c.execute('''
            CREATE TABLE IF NOT EXISTS answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL,
                answer_id INTEGER UNIQUE,
                body TEXT,
                score INTEGER,
                is_accepted BOOLEAN,
                creation_date INTEGER,
                owner_name TEXT,
                owner_reputation INTEGER,
                comment_count INTEGER,
                last_activity_date INTEGER,
                is_verified BOOLEAN DEFAULT 0,
                fetched_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (question_id) REFERENCES questions(id)
            )
        ''')
        # Таблица комментариев с уникальным ограничением на comment_id
        c.execute('''
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL,
                answer_id INTEGER,
                comment_id INTEGER UNIQUE,
                body TEXT,
                score INTEGER,
                creation_date INTEGER,
                owner_name TEXT,
                owner_reputation INTEGER,
                fetched_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (question_id) REFERENCES questions(id)
            )
        ''')
        # Добавление колонки enriched в существующую таблицу questions
        # (если таблица уже была создана без этой колонки)
        try:
            c.execute(
                "ALTER TABLE questions ADD COLUMN enriched INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            # Колонка уже существует --- игнорируем ошибку
            pass

        # Индекс для ускорения выборки ответов по конкретному вопросу
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_answers_question "
            "ON answers(question_id)"
        )
        # Индекс для ускорения выборки комментариев по конкретному ответу
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_comments_answer "
            "ON comments(answer_id)"
        )

        conn.commit()
        conn.close()
        logging.info("Схема базы данных приведена к целевому виду")

    def extract_question_id(self, link):
        """
        Извлекает числовой идентификатор вопроса из URL Stack Overflow.
        Пример: 'https://ru.stackoverflow.com/questions/12345/...' → '12345'
        :param link: полный URL вопроса
        :return: строка с числовым ID или None, если ID не найден
        """
        parts = link.split('/')
        for i, p in enumerate(parts):
            if p == "questions" and i + 1 < len(parts):
                return parts[i + 1]
        return None

    def get_unprocessed_questions(self):
        """
        Возвращает список вопросов, для которых ещё не загружены ответы
        (enriched = 0). Используется для возобновления прерванного сбора.
        :return: список кортежей (id, link, title)
        """
        conn = sqlite3.connect(self.db_path)
        rows = conn.cursor().execute(
            'SELECT id, link, title FROM questions WHERE enriched = 0'
        ).fetchall()
        conn.close()
        self.safe_print(f"Найдено {len(rows)} необработанных вопросов")
        return rows

    def _api_request(self, url, params):
        """
        Выполняет GET-запрос к Stack Exchange API с соблюдением ограничения
        частоты запросов и тайм-аутом 15 секунд. Повторные попытки при ошибках
        5xx обеспечиваются адаптером Retry на уровне сессии.
        :param url: полный URL эндпоинта API
        :param params: словарь параметров запроса
        :return: объект requests.Response
        :raises requests.exceptions.RequestException: при неустранимой ошибке
        """
        # Ожидаем разрешения ограничителя частоты запросов
        self.rate_limiter.acquire()
        try:
            r = self.session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            self.safe_print(f"Ошибка API-запроса к {url}: {e}")
            raise

    def fetch_answers_and_comments(self, question_id, stack_id):
        """
        Загружает все ответы и комментарии для одного вопроса.
        Является точкой входа для рабочих потоков ThreadPoolExecutor.
        Алгоритм:
         1. Запрос ответов: questions/{stack_id}/answers (сортировка по голосам)
         2. Для каждого ответа --- постановка в очередь DBWriter
         3. Пакетный запрос комментариев для всех ответов вопроса
         4. Установка флага enriched=1 в случае успеха
        :param question_id: локальный ID вопроса в нашей базе данных
        :param stack_id: числовой ID вопроса на Stack Overflow
        :return: True при успехе, False при ошибке
        """
        try:
            # Формируем параметры запроса ответов
            params = {
                'order': 'desc',
                'sort': 'votes',
                'site': self.site,
                'filter': 'withbody',   # включает полный текст ответа
                'pagesize': 100
            }
            if self.api_key:
                params['key'] = self.api_key

            url = f"{self.base_url}questions/{stack_id}/answers"
            r = self._api_request(url, params)
            data = r.json()
            answers = data.get('items', [])
            self.safe_print(
                f"Q{question_id}: получено {len(answers)} ответов, "
                f"остаток квоты={data.get('quota_remaining', '?')}"
            )

            if not answers:
                # Вопрос без ответов --- помечаем как обработанный
                self._mark_done(question_id)
                return True

            # Сохраняем ответы и собираем их ID для загрузки комментариев
            answer_ids = []
            for ans in answers:
                self._submit_answer(question_id, ans)
                answer_ids.append(ans['answer_id'])

            # Пакетная загрузка комментариев ко всем ответам вопроса
            self._fetch_comments(question_id, answer_ids)

            # Помечаем вопрос как полностью обработанный
            self._mark_done(question_id)
            return True

        except Exception as e:
            self.safe_print(f"Ошибка обработки Q{question_id}: {e}")
            # enriched остаётся 0 --- вопрос будет повторно обработан
            return False

    def _submit_answer(self, q_id, ans):
        """
        Формирует SQL-задание на вставку одного ответа и передаёт его DBWriter.
        Использует INSERT OR REPLACE для безопасного повторного запуска.
        Тело ответа декодируется из HTML-сущностей через html.unescape.
        :param q_id: локальный ID вопроса в нашей базе данных
        :param ans: словарь с данными ответа из JSON-ответа API
        """
        owner = ans.get('owner', {})
        sql = (
            'INSERT OR REPLACE INTO answers '
            '(question_id, answer_id, body, score, is_accepted, '
            'creation_date, owner_name, owner_reputation, '
            'comment_count, last_activity_date, is_verified) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
        )
        self.writer.submit(sql, (
            q_id,
            ans['answer_id'],
            html.unescape(ans.get('body') or ''),   # декодирование HTML-сущностей
            ans.get('score', 0),
            ans.get('is_accepted', False),
            ans.get('creation_date'),
            owner.get('display_name', 'Unknown'),
            owner.get('reputation', 0),
            ans.get('comment_count', 0),
            ans.get('last_activity_date', ans.get('creation_date')),
            ans.get('is_accepted', False)          # is_verified = is_accepted
        ))

    def _fetch_comments(self, q_id, answer_ids):
        """
        Загружает комментарии для списка ответов пакетным методом:
        API принимает до 100 ID ответов в одном запросе через точку с запятой.
        Реализована полная пагинация (цикл while has_more).
        :param q_id: локальный ID вопроса
        :param answer_ids: список числовых ID ответов для запроса комментариев
        """
        if not answer_ids:
            return

        # Разбиваем список на батчи по 100 идентификаторов
        for i in range(0, len(answer_ids), 100):
            chunk = answer_ids[i:i + 100]
            ids_str = ';'.join(str(a) for a in chunk)
            url = f"{self.base_url}answers/{ids_str}/comments"
            params = {
                'order': 'desc',
                'sort': 'creation',
                'site': self.site,
                'filter': 'withbody',
                'pagesize': 100
            }
            if self.api_key:
                params['key'] = self.api_key

            # Пагинация: запрашиваем страницы до тех пор, пока has_more = True
            page = 1
            has_more = True
            while has_more:
                params['page'] = page
                try:
                    data = self._api_request(url, params).json()
                except requests.exceptions.RequestException:
                    self.safe_print(
                        f"Не удалось загрузить комментарии для чанка {i//100 + 1}"
                    )
                    break
                for c in data.get('items', []):
                    self._submit_comment(q_id, c['post_id'], c)
                has_more = data.get('has_more', False)
                page += 1
                # Небольшая задержка между страницами пагинации
                if has_more:
                    time.sleep(0.2)

    def _submit_comment(self, q_id, a_id, c):
        """
        Формирует SQL-задание на вставку одного комментария и передаёт DBWriter.
        Использует INSERT OR REPLACE для идемпотентности.
        :param q_id: локальный ID вопроса
        :param a_id: ID ответа, к которому относится комментарий
        :param c: словарь с данными комментария из JSON-ответа API
        """
        owner = c.get('owner', {})
        sql = (
            'INSERT OR REPLACE INTO comments '
            '(question_id, answer_id, comment_id, body, score, '
            'creation_date, owner_name, owner_reputation) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)'
        )
        self.writer.submit(sql, (
            q_id,
            a_id,
            c['comment_id'],
            html.unescape(c.get('body') or ''),   # декодирование HTML-сущностей
            c.get('score', 0),
            c.get('creation_date'),
            owner.get('display_name', 'Unknown'),
            owner.get('reputation', 0)
        ))

    def _mark_done(self, q_id):
        """
        Устанавливает флаг enriched=1 для вопроса после успешной загрузки
        всех его ответов и комментариев. Обеспечивает контрольную точку:
        при повторном запуске этот вопрос будет пропущен.
        :param q_id: локальный ID вопроса в нашей базе данных
        """
        self.writer.submit(
            "UPDATE questions SET enriched = 1 WHERE id = ?",
            (q_id,)
        )

    def check_saved_data(self):
        """
        Выводит в лог текущую статистику базы данных:
        количество сохранённых ответов, комментариев и необработанных вопросов.
        Используется для мониторинга прогресса обогащения.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        ans = c.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        com = c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        unp = c.execute(
            "SELECT COUNT(*) FROM questions WHERE enriched = 0"
        ).fetchone()[0]
        conn.close()
        self.safe_print(
            f"Статистика БД → Ответов: {ans} | Комментариев: {com} | "
            f"Необработанных вопросов: {unp}"
        )

    def shutdown(self):
        """
        Корректно завершает работу обогатителя:
        закрывает HTTP-сессию и останавливает поток-писатель DBWriter
        (с ожиданием завершения финального сброса буфера).
        """
        self.session.close()
        self.writer.stop()
        logging.info("StackOverflowEnricher завершил работу")

# ── Точка входа ───────────────────────────────────────────────────────────────

def main():
    """
    Запускает процесс обогащения базы данных:
     1. Создаёт экземпляр StackOverflowEnricher
     2. Получает список необработанных вопросов
     3. Запускает многопоточную обработку через ThreadPoolExecutor (8 потоков)
     4. Периодически выводит статистику прогресса
     5. По завершении корректно останавливает все компоненты

    Для переключения на русскоязычную базу:
        DB_PATH = 'AAAA_stackoverflow_python.db'
        site='ru.stackoverflow'
    """
    # Настройки подключения к базе данных и API
    DB_PATH = "1q.db"
    API_KEY = "ВАШ_КЛЮЧ_API"   # замените на реальный ключ

    enricher = StackOverflowEnricher(DB_PATH, API_KEY, site='stackoverflow')
    questions = enricher.get_unprocessed_questions()

    if not questions:
        logging.info("Все вопросы уже обогащены!")
        enricher.shutdown()
        return

    total = len(questions)
    completed = 0

    # Многопоточная обработка: 8 потоков подобраны экспериментально
    # (при 15 потоках возникали эпизодические срабатывания ограничений API)
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_q = {}
        # Регистрируем задачи: для каждого вопроса извлекаем Stack ID из URL
        for q_id, link, title in questions:
            stack_id = enricher.extract_question_id(link)
            if stack_id:
                f = executor.submit(
                    enricher.fetch_answers_and_comments,
                    q_id,
                    stack_id
                )
                future_to_q[f] = q_id
            else:
                logging.warning(f"Не удалось извлечь Stack ID из ссылки: {link}")

        # Обрабатываем завершённые задачи по мере их готовности
        for future in as_completed(future_to_q):
            try:
                future.result()
            except Exception as e:
                logging.error(f"Необработанное исключение в потоке: {e}")
            completed += 1
            # Вывод статистики каждые 100 обработанных вопросов
            if completed % 100 == 0:
                enricher.check_saved_data()
                logging.info(f"Прогресс: {completed}/{total}")

    # Корректное завершение работы и финальная статистика
    enricher.shutdown()
    enricher.check_saved_data()
    logging.info(f"Обработано всего: {total} вопросов.")

if __name__ == "__main__":
    main()

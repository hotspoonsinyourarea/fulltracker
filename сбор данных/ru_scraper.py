# ru_scraper.py
# Модуль первого этапа ETL-конвейера: последовательный обход страниц
# ru.stackoverflow.com с тегом «python» и сохранение метаданных вопросов
# в локальную базу данных SQLite.

import sqlite3
import time
import logging
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup

# Настройка логирования: уровень INFO, формат с меткой времени
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def create_connection(db_file):
    """
    Создаёт и возвращает соединение с базой данных SQLite.
    При ошибке подключения возвращает None и записывает сообщение в лог.
    :param db_file: путь к файлу базы данных
    :return: объект соединения sqlite3.Connection или None
    """
    conn = None
    try:
        conn = sqlite3.connect(db_file)
        logging.info(f"Подключение к БД '{db_file}' установлено")
    except sqlite3.Error as e:
        logging.error(f"Ошибка подключения к БД: {e}")
    return conn

def create_table(conn):
    """
    Создаёт таблицу questions, если она ещё не существует.
    Включает поля: id, title, answers, votes, views, link (UNIQUE),
    description (зарезервировано), enriched (флаг обогащения через API).
    :param conn: активное соединение с базой данных
    """
    try:
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                answers INTEGER NOT NULL DEFAULT 0,
                votes INTEGER NOT NULL DEFAULT 0,
                views INTEGER NOT NULL DEFAULT 0,
                link TEXT NOT NULL UNIQUE,
                description TEXT,
                enriched INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        logging.info("Таблица 'questions' готова")
    except sqlite3.Error as e:
        logging.error(f"Ошибка создания таблицы: {e}")

def insert_questions_batch(conn, questions):
    """
    Выполняет пакетную вставку списка вопросов в таблицу questions.
    Использует INSERT OR IGNORE, чтобы повторные запуски не вызывали
    ошибок при нарушении ограничения UNIQUE на поле link.
    :param conn: активное соединение с базой данных
    :param questions: список кортежей (title, answers, votes, views, link)
    """
    if not questions:
        return
    # INSERT OR IGNORE --- безопасная идемпотентная вставка
    sql = (
        "INSERT OR IGNORE INTO questions(title, answers, votes, views, link) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    try:
        conn.cursor().executemany(sql, questions)
        conn.commit()
        logging.info(f"Вставлено/пропущено {len(questions)} записей")
    except sqlite3.Error as e:
        logging.error(f"Ошибка пакетной вставки: {e}")

def get_page_content(driver, url, retries=3):
    """
    Открывает указанный URL в браузере и ожидает загрузки карточек вопросов.
    При неудаче повторяет попытку до retries раз с задержкой 3 секунды.
    :param driver: экземпляр Selenium WebDriver
    :param url: адрес страницы для загрузки
    :param retries: максимальное число попыток (по умолчанию 3)
    :return: HTML-исходник страницы (str) или None при неудаче
    """
    for attempt in range(1, retries + 1):
        try:
            driver.get(url)
            # Ожидаем появления хотя бы одной карточки вопроса
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located(
                    (By.CLASS_NAME, "s-post-summary")
                )
            )
            return driver.page_source
        except TimeoutException:
            logging.warning(
                f"Таймаут на странице {url} (попытка {attempt}/{retries})"
            )
            time.sleep(3)
        except Exception as e:
            logging.error(f"Ошибка загрузки страницы {url}: {e}")
            time.sleep(3)
    return None

def _parse_stat_number(text):
    """
    Преобразует строковое значение метрики из карточки вопроса в целое число.
    Обрабатывает суффиксы: 'k' (в т.ч. десятичные '1.2k'), ' раз', '-'.
    При невозможности преобразования возвращает 0.
    :param text: строка метрики, например '1.2k', '1k', '123 раз', '42'
    :return: целое число или 0
    """
    t = text.replace('\u00a0', ' ').replace(' раз', '').replace('-', '').strip().lower()
    if 'k' in t:
        try:
            return int(float(t.replace('k', '')) * 1000)
        except ValueError:
            return 0
    return int(t) if t.isdigit() else 0

def parse_questions(html):
    """
    Парсит HTML-страницу списка вопросов Stack Overflow и извлекает
    из каждой карточки: заголовок, число ответов, голосов, просмотров и ссылку.
    Структура карточки:
    <div class="s-post-summary">
      <h3 class="s-post-summary--content-title">
        <a class="s-link" href="/questions/12345/...">Заголовок</a>
      </h3>
      <div class="s-post-summary--stats-item"> ← голоса
      <div class="s-post-summary--stats-item"> ← ответы
      <div class="s-post-summary--stats-item"> ← просмотры
    :param html: HTML-код страницы (str)
    :return: список кортежей (title, answers, votes, views, link)
    """
    soup = BeautifulSoup(html, 'html.parser')
    questions = []
    for summary in soup.find_all('div', class_='s-post-summary'):
        try:
            # Извлекаем ссылку и заголовок из блока заголовка
            title_tag = summary.find(
                'h3', class_='s-post-summary--content-title'
            )
            if title_tag is None:
                continue
            a = title_tag.find('a', class_='s-link')
            if a is None:
                continue
            title = a.text.strip()
            link = "https://ru.stackoverflow.com" + a['href']

            # Извлекаем блоки со статистическими показателями
            stats = summary.find_all('div', class_='s-post-summary--stats-item')

            def get_num(el):
                """Вспомогательная функция: извлекает число из блока статистики."""
                span = el.find(
                    'span', class_='s-post-summary--stats-item-number'
                )
                if span is None:
                    return 0
                return _parse_stat_number(span.text.strip())

            # Определяем тип метрики по атрибуту title (голоса/ответы/просмотры)
            votes = answers = views = 0
            matched = False
            for stat in stats:
                stitle = (stat.get('title') or '').lower()
                num = get_num(stat)
                if any(kw in stitle for kw in ('голос', 'vote', 'score')):
                    votes = num; matched = True
                elif any(kw in stitle for kw in ('ответ', 'answer')):
                    answers = num; matched = True
                elif any(kw in stitle for kw in ('просмотр', 'view')):
                    views = num; matched = True

            # Fallback: если title не распознан — по позиции (голоса→ответы→просмотры)
            if not matched:
                votes = get_num(stats[0]) if len(stats) > 0 else 0
                answers = get_num(stats[1]) if len(stats) > 1 else 0
                views = get_num(stats[2]) if len(stats) > 2 else 0

            questions.append((title, answers, votes, views, link))
        except Exception as e:
            logging.error(f"Ошибка парсинга карточки вопроса: {e}")
    return questions

def main(db_file, base_url, num_pages, delay=1.5):
    """
    Основная функция скрапера: обходит num_pages страниц списка вопросов
    и сохраняет данные в базу данных SQLite.
    Между запросами выдерживается задержка delay секунд для уменьшения
    нагрузки на сервер и снижения риска блокировки.
    :param db_file: путь к файлу базы данных SQLite
    :param base_url: базовый URL страницы со списком вопросов
    :param num_pages: общее количество страниц для обхода
    :param delay: задержка в секундах между запросами (по умолчанию 1.5)
    """
    # Подключение к БД и создание таблицы
    conn = create_connection(db_file)
    if conn is None:
        logging.error("Не удалось подключиться к БД. Завершение работы.")
        return
    create_table(conn)

    # Настройка браузера Chrome в режиме headless
    opts = Options()
    for arg in [
        "--incognito",
        "--disable-extensions",
        "--blink-settings=imagesEnabled=false",  # отключение изображений — экономия трафика
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]:
        opts.add_argument(arg)

    driver = webdriver.Chrome(options=opts)
    try:
        for page in range(1, num_pages + 1):
            sep = '&' if '?' in base_url else '?'
            url = f"{base_url}{sep}page={page}"
            logging.info(f"Обрабатывается страница {page}/{num_pages}: {url}")
            html = get_page_content(driver, url)
            if html:
                parsed = parse_questions(html)
                if parsed:
                    insert_questions_batch(conn, parsed)
                else:
                    logging.warning(f"Страница {page}: вопросов не найдено")
            else:
                logging.warning(f"Страница {page}: не удалось получить HTML")
            # Задержка между запросами для соблюдения этических норм сбора
            time.sleep(delay)
    finally:
        # Гарантированное закрытие браузера и соединения с БД
        driver.quit()
        conn.close()
        logging.info("Скрапер завершил работу")

if __name__ == "__main__":
    main(
        db_file='AAAA_stackoverflow_python.db',
        base_url=(
            'https://ru.stackoverflow.com/questions/tagged/python'
            '?tab=Newest'
        ),
        num_pages=4456,
        delay=1.5
    )

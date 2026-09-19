"""
Скачивание глав "Теневой Раб" (Shadow Slave) с telegra.ph

Как работает:
1. Начинает со страницы тома (по умолчанию 5 том, откуда идёт чистый telegra.ph без Ranobelib)
2. Собирает все ссылки "Глава N" на странице тома
3. Идёт по ссылке "Следующий том" и повторяет, пока тома не закончатся
4. Все главы, найденные через тома, скачивает параллельно (несколько потоков) —
   их url заранее известны, поэтому порядок скачивания не важен
5. После последней главы, найденной через тома (например, 1840), тома заканчиваются,
   но у самих глав в конце текста есть ссылка "Следующая глава". Url следующей главы
   узнаётся только со страницы предыдущей, поэтому обход одной цепочки принципиально
   последовательный и его нельзя распараллелить. Но если заранее известны url ещё
   каких-то более поздних глав (см. CHAIN_CHECKPOINTS ниже), от каждой такой
   контрольной точки запускается СВОЯ независимая цепочка, и все они идут
   ПАРАЛЛЕЛЬНО, пока не упрутся в следующую контрольную точку или в конец истории.
   Каждая страница при этом запрашивается только один раз (сразу и текст
   сохраняется, и ищется ссылка на следующую).
6. Прогресс каждой цепочки (до какой главы дошли) сохраняется в
   chapters/chain_progress.json, поэтому при перезапуске скрипт не начинает
   поиск заново, а продолжает с сохранённого места
7. Каждую главу скачивает через официальный API Telegraph (api.telegra.ph/getPage)
8. Сохраняет каждую главу в отдельный .txt файл в папке chapters/

Установка зависимостей:
    pip install requests beautifulsoup4

Запуск:
    python download_chapters.py
"""

import re
import json
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

START_VOLUME_URL = "https://telegra.ph/5-tom-989-1060-04-29"
OUTPUT_DIR = Path("chapters")
PROGRESS_FILE = OUTPUT_DIR / "chain_progress.json"
MAX_WORKERS = 8  # потоков для скачивания уже известных глав (из томов)
VOLUME_DELAY = 0.5  # секунды между чтением страниц томов (их всего десяток, не критично)

# Главы после последнего тома известны только по цепочке "Следующая глава",
# и это последовательный процесс. Чтобы не идти по одной длинной цепочке от
# 1840 до самого конца истории, здесь можно перечислить url уже известных
# более поздних глав — от каждой из них запустится своя параллельная цепочка.
# Ключ — номер главы, значение — её url.
CHAIN_CHECKPOINTS = {
    2250: "https://telegra.ph/Glava-2250-Plamya-nadezhdy-04-03",
    3000: "https://telegra.ph/Glava-3000-Vospominaniya-zabveniya-05-25",
}

CHAPTER_LINK_RE = re.compile(r"Глава\s+(\d+)")
NEXT_VOLUME_RE = re.compile(r"Следующий\s+том", re.IGNORECASE)
NEXT_CHAPTER_LINK_TEXT_RE = re.compile(r"след", re.IGNORECASE)

_thread_local = threading.local()


def get_session() -> requests.Session:
    """Session на поток: переиспользует TCP/TLS-соединение вместо того, чтобы
    устанавливать его заново на каждый запрос — для длинной последовательной
    цепочки "Следующая глава" это заметно быстрее обычных requests.get()."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


def get_html(url: str) -> str:
    r = get_session().get(url, timeout=20)
    r.raise_for_status()
    return r.text


def extract_links_from_volume(html: str, base_url: str):
    """Возвращает (список (номер_главы, url), url_следующего_тома_или_None)"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    chapters = []
    next_volume_url = None

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not href.startswith("http"):
            href = "https://telegra.ph" + href

        m = CHAPTER_LINK_RE.search(text)
        if m:
            chapters.append((int(m.group(1)), href))
        elif NEXT_VOLUME_RE.search(text):
            next_volume_url = href

    return chapters, next_volume_url


def telegraph_path_from_url(url: str) -> str:
    """Из https://telegra.ph/Glava-989-... достаём path 'Glava-989-...'"""
    return urlparse(url).path.lstrip("/")


def node_to_text(node) -> str:
    """Рекурсивно вытаскивает текст из content-нод Telegraph API"""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        tag = node.get("tag")
        children = node.get("children", [])
        text = "".join(node_to_text(c) for c in children)
        if tag == "p":
            return text + "\n\n"
        if tag in ("br",):
            return "\n"
        return text
    if isinstance(node, list):
        return "".join(node_to_text(n) for n in node)
    return ""


def fetch_chapter_page(chapter_url: str) -> dict:
    """Возвращает result из Telegraph API (getPage) для главы: title, content и т.д."""
    path = telegraph_path_from_url(chapter_url)
    api_url = f"https://api.telegra.ph/getPage/{path}?return_content=true"
    r = get_session().get(api_url, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegraph API error for {chapter_url}: {data}")
    return data["result"]


def download_chapter_text(chapter_url: str) -> tuple[str, str]:
    """Возвращает (заголовок, текст_главы)"""
    result = fetch_chapter_page(chapter_url)
    title = result.get("title", telegraph_path_from_url(chapter_url))
    content = result.get("content", [])
    text = node_to_text(content).strip()
    return title, text


def find_links_in_content(content):
    """Собирает все ссылки (текст, url) из content-нод Telegraph API"""
    links = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "a":
                href = node.get("attrs", {}).get("href")
                if href:
                    if not href.startswith("http"):
                        href = "https://telegra.ph" + href
                    links.append((node_to_text(node.get("children", [])).strip(), href))
            for child in node.get("children", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(content)
    return links


def find_next_chapter_url(content):
    """Ищет в тексте главы ссылку "Следующая глава" и возвращает её url, если есть.

    После ~1840 главы больше не входят в тома-оглавления: единственный способ
    узнать про следующую главу — пройти по этой ссылке в конце текущей.
    """
    for text, href in find_links_in_content(content):
        if NEXT_CHAPTER_LINK_TEXT_RE.search(text):
            return href
    return None


def save_chapter(num: int, title: str, text: str):
    out_path = OUTPUT_DIR / f"{num:05d}.txt"
    out_path.write_text(f"{title}\n\n{text}", encoding="utf-8")
    print(f"  Глава {num}: сохранено ({len(text)} символов)")


def download_known_chapters(all_chapters: dict, max_workers: int = MAX_WORKERS):
    """Скачивает уже известные (из томов) главы параллельно в несколько потоков —
    их url заранее известны, порядок скачивания не важен."""
    todo = [
        (num, url)
        for num, url in all_chapters.items()
        if not (OUTPUT_DIR / f"{num:05d}.txt").exists()
    ]
    if not todo:
        print("  Все известные главы уже скачаны.")
        return

    print(f"  Нужно скачать {len(todo)} глав(ы), потоков: {max_workers}")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_chapter_text, url): num for num, url in todo}
        for future in as_completed(futures):
            num = futures[future]
            try:
                title, text = future.result()
            except Exception as e:
                print(f"  Глава {num}: ОШИБКА ({e}), пропускаю")
                continue
            save_chapter(num, title, text)


def load_progress() -> dict:
    """Читает сохранённый прогресс цепочек: до какой главы дошла каждая из них."""
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def build_chain_segments(all_chapters: dict) -> list[tuple[int, str, "int | None"]]:
    """Строит список независимых отрезков цепочки "Следующая глава".

    Каждый отрезок начинается в известной точке (последняя глава из томов или
    одна из CHAIN_CHECKPOINTS) и обрывается ровно там, где начинается следующая
    точка — эту главу скачает уже следующий отрезок, а не текущий, чтобы никакая
    глава не считалась "скачанной" дважды и ни одна не пропускалась на стыке.
    """
    anchors: list[tuple[int, str]] = []
    if all_chapters:
        last_num = max(all_chapters)
        anchors.append((last_num, all_chapters[last_num]))
    for num, url in CHAIN_CHECKPOINTS.items():
        if not anchors or num > anchors[0][0]:
            anchors.append((num, url))
    anchors.sort(key=lambda a: a[0])

    segments = []
    for i, (num, url) in enumerate(anchors):
        stop_num = anchors[i + 1][0] if i + 1 < len(anchors) else None
        segments.append((num, url, stop_num))
    return segments


def run_chain_segment(anchor_num: int, anchor_url: str, stop_num, progress: dict, progress_lock: threading.Lock):
    """Идёт по ссылке "Следующая глава", начиная с anchor_num, и сразу сохраняет
    каждую главу. Останавливается перед stop_num (её скачает соседний отрезок)
    либо когда ссылка на следующую главу больше не находится.

    Если в PROGRESS_FILE уже есть более поздняя позиция для этого же anchor_num
    (с прошлого запуска), продолжает с неё, а не с самого начала отрезка —
    так не приходится заново проходить уже пройденные главы после перезапуска.
    """
    key = str(anchor_num)
    saved = progress.get(key)
    if saved and saved.get("last_num", anchor_num) >= anchor_num:
        num, url = saved["last_num"], saved["last_url"]
        if stop_num is not None and num >= stop_num:
            return num, 0  # этот отрезок уже был пройден целиком в прошлый раз
        print(f"  [{anchor_num}] продолжаю с сохранённой позиции: глава {num}")
    else:
        num, url = anchor_num, anchor_url

    seen_urls = {url}
    found = 0
    last_processed_num = None  # последняя глава, которую этот отрезок реально скачал

    while True:
        try:
            result = fetch_chapter_page(url)
        except Exception as e:
            print(f"  [{anchor_num}] глава {num}: не удалось прочитать ({e})")
            break

        out_path = OUTPUT_DIR / f"{num:05d}.txt"
        if not out_path.exists():
            title = result.get("title", telegraph_path_from_url(url))
            text = node_to_text(result.get("content", [])).strip()
            save_chapter(num, title, text)
        last_processed_num = num

        with progress_lock:
            progress[key] = {"last_num": num, "last_url": url}
            PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")

        next_url = find_next_chapter_url(result.get("content", []))
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)

        num += 1
        if stop_num is not None and num >= stop_num:
            break  # эту и дальнейшие главы скачает отрезок, начинающийся в stop_num
        found += 1
        url = next_url

    return (last_processed_num if last_processed_num is not None else num), found


def run_chain_segments(all_chapters: dict):
    """Запускает все отрезки цепочки "Следующая глава" параллельно, каждый в
    своём потоке — они независимы (используют разные url), поэтому в отличие
    от обхода одной цепочки это можно безопасно распараллелить."""
    segments = build_chain_segments(all_chapters)
    if not segments:
        return

    progress = load_progress()
    progress_lock = threading.Lock()

    print(f"  Запускаю {len(segments)} параллельных цепочек «Следующая глава»...")
    with ThreadPoolExecutor(max_workers=len(segments)) as executor:
        futures = {
            executor.submit(run_chain_segment, num, url, stop_num, progress, progress_lock): num
            for num, url, stop_num in segments
        }
        for future in as_completed(futures):
            anchor_num = futures[future]
            try:
                last_num, found = future.result()
            except Exception as e:
                print(f"  [{anchor_num}] цепочка прервалась с ошибкой: {e}")
                continue
            print(f"  [{anchor_num}] цепочка дошла до главы {last_num} (+{found} новых)")


def build_index():
    """Пересобирает chapters/index.json из того, что реально лежит на диске.

    Этот файл — единственный способ для читалки (index.html) узнать, какие
    главы вообще существуют: она больше не ходит в telegra.ph сама, а просто
    читает статические файлы из chapters/, которые готовит этот скрипт.
    """
    entries = []
    for path in sorted(OUTPUT_DIR.glob("*.txt")):
        try:
            num = int(path.stem)
        except ValueError:
            continue
        title = path.read_text(encoding="utf-8").split("\n", 1)[0]
        entries.append({"num": num, "title": title, "file": path.name})
    entries.sort(key=lambda e: e["num"])

    index_path = OUTPUT_DIR / "index.json"
    index_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Индекс глав обновлён: {index_path} ({len(entries)} глав)")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    all_chapters = {}  # номер -> url
    volume_url = START_VOLUME_URL
    seen_volumes = set()

    try:
        print("Собираю ссылки на главы по томам...")
        while volume_url and volume_url not in seen_volumes:
            seen_volumes.add(volume_url)
            print(f"  Читаю оглавление: {volume_url}")
            html = get_html(volume_url)
            chapters, next_volume_url = extract_links_from_volume(html, volume_url)
            for num, url in chapters:
                all_chapters[num] = url
            print(f"    Найдено {len(chapters)} глав. Следующий том: {next_volume_url}")
            volume_url = next_volume_url
            time.sleep(VOLUME_DELAY)

        print(f"\nНайдено глав по томам: {len(all_chapters)}")

        print("\nСкачиваю главы из томов (параллельно)...")
        download_known_chapters(all_chapters)

        print("\nИщу и скачиваю главы после последнего тома по ссылке «Следующая глава»...")
        run_chain_segments(all_chapters)

        print("\nГотово. Файлы лежат в папке chapters/")
    finally:
        # индекс пересобираем всегда, даже если скрипт прервали (Ctrl+C, обрыв
        # сети) — читалка должна видеть все главы, что реально скачаны на диск
        build_index()


if __name__ == "__main__":
    main()

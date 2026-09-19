"""
Скачивание глав "Теневой Раб" (Shadow Slave) с telegra.ph

Как работает:
1. Начинает со страницы тома (по умолчанию 5 том, откуда идёт чистый telegra.ph без Ranobelib)
2. Собирает все ссылки "Глава N" на странице тома
3. Идёт по ссылке "Следующий том" и повторяет, пока тома не закончатся
4. Все главы, найденные через тома, скачивает параллельно (несколько потоков) —
   их url заранее известны, поэтому порядок скачивания не важен
5. После последней главы, найденной через тома (например, 1840), тома заканчиваются,
   но у самих глав в конце текста есть ссылка "Следующая глава". Такие главы скачивает
   по одной, идя по этой ссылке: url следующей главы становится известен только со
   страницы предыдущей, поэтому этот шаг принципиально последовательный и его нельзя
   распараллелить — но каждая страница запрашивается только один раз (сразу и текст
   сохраняется, и ищется ссылка на следующую), а не дважды, как раньше
6. Каждую главу скачивает через официальный API Telegraph (api.telegra.ph/getPage)
7. Сохраняет каждую главу в отдельный .txt файл в папке chapters/

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
MAX_WORKERS = 8  # потоков для скачивания уже известных глав (из томов)
VOLUME_DELAY = 0.5  # секунды между чтением страниц томов (их всего десяток, не критично)

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


def follow_and_download_next_links(all_chapters: dict):
    """Продолжает за пределы последнего тома, идя по ссылке "Следующая глава"
    внутри самих глав, и сразу сохраняет каждую главу.

    Url следующей главы узнаётся только со страницы текущей, поэтому это
    принципиально последовательный процесс (его нельзя распараллелить, не зная
    заранее url'ы) — но каждая страница запрашивается один раз, а не дважды:
    из того же ответа API берётся и текст для сохранения, и ссылка на следующую.
    """
    if not all_chapters:
        return

    num = max(all_chapters)
    url = all_chapters[num]
    seen_urls = {url}
    found = 0

    while True:
        try:
            result = fetch_chapter_page(url)
        except Exception as e:
            print(f"  Глава {num}: не удалось прочитать ({e})")
            break

        out_path = OUTPUT_DIR / f"{num:05d}.txt"
        if not out_path.exists():
            title = result.get("title", telegraph_path_from_url(url))
            text = node_to_text(result.get("content", [])).strip()
            save_chapter(num, title, text)

        next_url = find_next_chapter_url(result.get("content", []))
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)

        num += 1
        if num in all_chapters:
            break
        all_chapters[num] = next_url
        found += 1
        url = next_url

    if found:
        print(f"  Найдено и скачано ещё {found} глав(ы) по ссылке «Следующая глава»")


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
        follow_and_download_next_links(all_chapters)

        print("\nГотово. Файлы лежат в папке chapters/")
    finally:
        # индекс пересобираем всегда, даже если скрипт прервали (Ctrl+C, обрыв
        # сети) — читалка должна видеть все главы, что реально скачаны на диск
        build_index()


if __name__ == "__main__":
    main()

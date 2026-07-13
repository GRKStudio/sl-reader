"""
Скачивание глав "Теневой Раб" (Shadow Slave) с telegra.ph

Как работает:
1. Начинает со страницы тома (по умолчанию 5 том, откуда идёт чистый telegra.ph без Ranobelib)
2. Собирает все ссылки "Глава N" на странице тома
3. Идёт по ссылке "Следующий том" и повторяет, пока тома не закончатся
4. Каждую главу скачивает через официальный API Telegraph (api.telegra.ph/getPage)
5. Сохраняет каждую главу в отдельный .txt файл в папке chapters/

Установка зависимостей:
    pip install requests beautifulsoup4

Запуск:
    python download_chapters.py
"""

import re
import time
import json
import requests
from pathlib import Path
from urllib.parse import urlparse

START_VOLUME_URL = "https://telegra.ph/5-tom-989-1060-04-29"
OUTPUT_DIR = Path("chapters")
DELAY_BETWEEN_REQUESTS = 0.5  # секунды, чтобы не спамить API

CHAPTER_LINK_RE = re.compile(r"Глава\s+(\d+)")
NEXT_VOLUME_RE = re.compile(r"Следующий\s+том", re.IGNORECASE)


def get_html(url: str) -> str:
    r = requests.get(url, timeout=20)
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


def download_chapter_text(chapter_url: str) -> tuple[str, str]:
    """Возвращает (заголовок, текст_главы)"""
    path = telegraph_path_from_url(chapter_url)
    api_url = f"https://api.telegra.ph/getPage/{path}?return_content=true"
    r = requests.get(api_url, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegraph API error for {chapter_url}: {data}")

    result = data["result"]
    title = result.get("title", path)
    content = result.get("content", [])
    text = node_to_text(content).strip()
    return title, text


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    all_chapters = {}  # номер -> url
    volume_url = START_VOLUME_URL
    seen_volumes = set()

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
        time.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"\nВсего найдено глав: {len(all_chapters)}")

    print("\nСкачиваю главы...")
    for num in sorted(all_chapters):
        out_path = OUTPUT_DIR / f"{num:05d}.txt"
        if out_path.exists():
            continue  # уже скачано, можно перезапускать скрипт после обрыва
        url = all_chapters[num]
        try:
            title, text = download_chapter_text(url)
        except Exception as e:
            print(f"  Глава {num}: ОШИБКА ({e}), пропускаю")
            continue

        out_path.write_text(f"{title}\n\n{text}", encoding="utf-8")
        print(f"  Глава {num}: сохранено ({len(text)} символов)")
        time.sleep(DELAY_BETWEEN_REQUESTS)

    print("\nГотово. Файлы лежат в папке chapters/")


if __name__ == "__main__":
    main()

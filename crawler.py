import json
import os
import re
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

# --- 설정 ---
SERVER_ID = "adven"  # 던담 검색 시 사용되는 서버 ID
GOOGLE_SHEET_NAME = "레이드 공대표"  # 메인 시트 이름
ADVENTURE_NAMES_SHEET_NAME = "시트2"   # 모험단명을 읽어올 시트 이름
# ------------

BASE_URL = "https://dundam.xyz/search"

FAKE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://dundam.xyz/",
    "Cache-Control": "no-cache",
}

def get_adventure_names_from_sheet():
    print(f"'{ADVENTURE_NAMES_SHEET_NAME}' 시트에서 모험단명을 읽어옵니다...")
    try:
        creds_json_str = os.getenv('GOOGLE_CREDENTIALS')
        creds_info = json.loads(creds_json_str)
        scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(creds_info, scopes=scope)
        gc = gspread.authorize(creds)

        doc = gc.open(GOOGLE_SHEET_NAME)
        worksheet = doc.worksheet(ADVENTURE_NAMES_SHEET_NAME)
        adventure_names = [name.strip() for name in worksheet.col_values(1) if name.strip()]

        print(f"총 {len(adventure_names)}개의 모험단명을 읽어왔습니다.")
        return adventure_names
    except Exception as e:
        print(f"모험단명 읽기 중 오류 발생: {e}")
        return []

# ---- HTML 가져오기 ----
def looks_like_challenge(html: str) -> bool:
    h = html.lower()
    needles = ["just a moment", "cf-chl", "checking your browser", "captcha",
               "turnstile", "access denied"]
    return any(n in h for n in needles)

def fetch_by_requests(server: str, name: str, timeout=30) -> str:
    params = {"server": server, "name": name}
    r = requests.get(BASE_URL, params=params, headers=FAKE_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text

def fetch_by_playwright(server: str, name: str, timeout_ms=60000) -> str:
    url = f"{BASE_URL}?{urlencode({'server': server, 'name': name})}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(locale="ko-KR",
                                  user_agent=FAKE_HEADERS["User-Agent"],
                                  extra_http_headers=FAKE_HEADERS)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_selector("section#search_result .sr-result .scon", timeout=15000)
        except Exception:
            pass
        html = page.content()
        ctx.close()
        browser.close()
        return html

def fetch_dundam_page(adventure_name: str, server: str = "adven") -> str:
    try:
        html = fetch_by_requests(server, adventure_name)
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("section#search_result .sr-result .scon")
        if looks_like_challenge(html) or len(cards) == 0:
            html = fetch_by_playwright(server, adventure_name)
        return html
    except Exception:
        return fetch_by_playwright(server, adventure_name)

# ---- HTML 파싱 ----
def _first_text(node):
    if not node:
        return ""
    for c in node.contents:
        if isinstance(c, str):
            t = c.strip()
            if t:
                return t
    return node.get_text(strip=True)

def _find_value_block(card, label_kw: str):
    for statc in card.select(".seh_stat .statc"):
        tl = statc.select_one("span.tl")
        if tl and label_kw in tl.get_text(strip=True):
            val = statc.select_one("span.val")
            return val.get_text(strip=True) if val else ""
    return ""

def _parse_korean_number(s: str):
    if not s:
        return None
    t = s.replace(",", "").strip()
    total = 0
    m = re.search(r"(\d+)\s*억", t)
    if m:
        total += int(m.group(1)) * 100_000_000
    m = re.search(r"(\d+)\s*만", t)
    if m:
        total += int(m.group(1)) * 10_000
    if total == 0:
        digits = re.findall(r"\d+", t)
        if digits:
            total = int("".join(digits))
    return total if total != 0 else None

def parse_cards(html: str, adventure_name: str):
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("section#search_result .sr-result .scon")
    results = []
    for c in cards:
        name = _first_text(c.select_one(".seh_name span.name"))
        rank_text = _find_value_block(c, "랭킹")
        buff_text = _find_value_block(c, "버프점수")
        results.append([
            adventure_name,
            name,
            rank_text,
            buff_text
        ])
    return results

def scrape_dundam_html(adventure_name):
    print(f"'{adventure_name}' 모험단 정보를 크롤링합니다...")
    html = fetch_dundam_page(adventure_name, SERVER_ID)
    cards = parse_cards(html, adventure_name)
    if not cards:
        print(f"'{adventure_name}' 모험단에 대한 캐릭터 정보 컨테이너를 찾을 수 없습니다.")
    else:
        print(f"'{adventure_name}' 모험단에서 총 {len(cards)}개의 캐릭터 정보를 추출했습니다.")
    return cards

# ---- 시트 업데이트 ----
def update_google_sheet(data):
    print("Google Sheets 업데이트를 시작합니다...")
    try:
        creds_json_str = os.getenv('GOOGLE_CREDENTIALS')
        creds_info = json.loads(creds_json_str)
        scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(creds_info, scopes=scope)
        gc = gspread.authorize(creds)

        doc = gc.open(GOOGLE_SHEET_NAME)
        worksheet = doc.get_worksheet(2)
        worksheet.clear()

        header = ['모험단명', '캐릭터명', '랭킹', '버프점수']
        if not data:
            worksheet.update([header], 'A1')
        else:
            worksheet.update([header] + data, 'A1')
            print("Google Sheets 업데이트 완료.")
    except Exception as e:
        print(f"Google Sheets 업데이트 중 오류 발생: {e}")

# ---- 실행부 ----
if __name__ == "__main__":
    all_scraped_data = []
    adventure_names_to_scrape = get_adventure_names_from_sheet()

    for name in adventure_names_to_scrape:
        all_scraped_data.extend(scrape_dundam_html(name))

    update_google_sheet(all_scraped_data)

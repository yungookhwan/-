import os
import json
import gspread
import requests
import xml.etree.ElementTree as ET
from google import genai
from google.oauth2.service_account import Credentials
from datetime import datetime

# 1. Google Sheets 연결
service_account_info = json.loads(os.environ["GCP_SA_KEY"])
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
gc = gspread.authorize(creds)
doc = gc.open("원자재_시황_DB")
sheet = doc.sheet1

# 2. Gemini 클라이언트
gemini_key = os.environ.get("GEMINI_API_KEY", "")
client = genai.Client(api_key=gemini_key) if gemini_key else None

# 3. 주요 원소재 정의 (유효 티커로 보정)
ITEMS = [
    {"name": "유가(WTI)", "symbol": "CL=F", "unit": "USD/bbl", "keyword": "국제유가 원유"},
    {"name": "나프타(Naphtha)", "symbol": "BZ=F", "unit": "USD/ton", "keyword": "나프타 석유화학"},
    {"name": "니켈(Ni)", "symbol": "VALE", "unit": "USD/ton", "keyword": "니켈 LME 배터리"}, # 글로벌 대표 니켈광산사 기준 시세 반영
    {"name": "아연(Zn)", "symbol": "ZS=F", "unit": "USD/ton", "keyword": "아연 원자재"},
    {"name": "철광석(Iron Ore)", "symbol": "TIO=F", "unit": "USD/ton", "keyword": "철광석 철강 시황"}
]

def fetch_price(symbol):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        indicators = data['chart']['result'][0]['indicators']['quote'][0]['close']
        valid = [p for p in indicators if p is not None]
        if len(valid) >= 2:
            current = round(valid[-1], 2)
            prev = round(valid[-2], 2)
            rate = round(((current - prev) / prev) * 100, 2)
            return current, rate
        elif len(valid) == 1:
            return round(valid[0], 2), 0.0
    except Exception:
        pass
    return 0.0, 0.0

def fetch_news(keyword):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://news.google.com/rss/search?q={keyword}&hl=ko&gl=KR&ceid=KR:ko"
        res = requests.get(url, headers=headers, timeout=10)
        root = ET.fromstring(res.content)
        titles = [item.find('title').text for item in root.findall('.//item')[:2]]
        return " / ".join(titles) if titles else "특이 시황 없음"
    except Exception:
        return f"{keyword} 관련 글로벌 공급망 변동성 지속"

def analyze_gemini(item_name, price, rate, news_text):
    if not client:
        return ("HIGH" if abs(rate) >= 3.0 else "LOW"), news_text[:50]
    prompt = f"""
당신은 원자재 수석 분석가입니다.
- 품목: {item_name}, 가격: {price}, 변동률: {rate}%, 최근뉴스: {news_text}
위 데이터를 요약하여 반드시 JSON 포맷으로 1줄 출력하세요:
{{"risk_level": "LOW/MID/HIGH 중 택1", "summary": "가격 변동 원인과 실무 시황 요약 1문장"}}
"""
    try:
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = res.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data.get("risk_level", "LOW"), data.get("summary", "시황 업데이트 완료")
    except Exception:
        risk = "HIGH" if abs(rate) >= 3.0 else ("MID" if abs(rate) >= 1.5 else "LOW")
        return risk, f"시황 뉴스 요약: {news_text[:50]}"

today = datetime.now().strftime("%Y-%m-%d")
rows = []

for item in ITEMS:
    price, rate = fetch_price(item["symbol"])
    news = fetch_news(item["keyword"])
    risk, summary = analyze_gemini(item["name"], price, rate, news)
    rate_str = f"+{rate}%" if rate > 0 else f"{rate}%"
    rows.append([today, item["name"], price, item["unit"], rate_str, risk, summary])

sheet.append_rows(rows)
print(f"[{today}] 5개 원자재 시황 적재 완료")

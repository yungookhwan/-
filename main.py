import os
import json
import gspread
import requests
import feedparser
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

# 3. 5대 주요 원소재 정의
ITEMS = [
    {"name": "유가(WTI)", "symbol": "CL=F", "unit": "USD/bbl", "keyword": "국제유가 원유 시황"},
    {"name": "나프타(Naphtha)", "symbol": "BZ=F", "unit": "USD/ton", "keyword": "나프타 석유화학 시황"},
    {"name": "니켈(Ni)", "symbol": "LN1=F", "unit": "USD/ton", "keyword": "니켈 LME 시황"},
    {"name": "아연(Zn)", "symbol": "ZS=F", "unit": "USD/ton", "keyword": "아연 비철금속 시황"},
    {"name": "철광석(Iron Ore)", "symbol": "TIO=F", "unit": "USD/ton", "keyword": "철광석 철강 시황"}
]

def fetch_price(symbol):
    """Yahoo Finance API를 직접 호출하여 안전하게 시세 조회"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        
        indicators = data['chart']['result'][0]['indicators']['quote'][0]['close']
        valid_prices = [p for p in indicators if p is not None]
        
        if len(valid_prices) >= 2:
            current = round(valid_prices[-1], 2)
            prev = round(valid_prices[-2], 2)
            rate = round(((current - prev) / prev) * 100, 2)
            return current, rate
        elif len(valid_prices) == 1:
            return round(valid_prices[0], 2), 0.0
    except Exception as e:
        print(f"[{symbol}] 시세 수집 오류: {e}")
    return 0.0, 0.0

def fetch_news(keyword):
    """구글 뉴스 RSS 최신 헤드라인 수집"""
    try:
        url = f"https://news.google.com/rss/search?q={keyword}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)
        titles = [e.title for e in feed.entries[:2]]
        return " / ".join(titles) if titles else "특이 시황 뉴스 없음"
    except Exception:
        return "뉴스 수집 불가"

def analyze_gemini(item_name, price, rate, news_text):
    """Gemini AI 요약 및 리스크 판정"""
    if not client:
        return ("HIGH" if abs(rate) >= 3.0 else "LOW"), f"뉴스: {news_text[:60]}"
    
    prompt = f"""
당신은 원자재 수석 분석가입니다. 아래 데이터를 바탕으로 변동 원인을 실무적으로 1문장 요약하세요.
- 품목: {item_name}, 단가: {price}, 변동률: {rate}%
- 관련뉴스: {news_text}

반드시 아래 JSON 형식으로만 1줄로 출력하세요:
{{"risk_level": "LOW/MID/HIGH 중 택1", "summary": "변동 원인 및 핵심 시황 1문장"}}
"""
    try:
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        cleaned = res.text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
        return parsed.get("risk_level", "LOW"), parsed.get("summary", "시황 업데이트 완료")
    except Exception as e:
        print(f"[{item_name}] Gemini 분석 fallback: {e}")
        risk = "HIGH" if abs(rate) >= 3.0 else ("MID" if abs(rate) >= 1.5 else "LOW")
        return risk, f"시황 뉴스 요약: {news_text[:50]}"

today = datetime.now().strftime("%Y-%m-%d")
rows = []

print("--- 원자재 데이터 수집 및 AI 요약 시작 ---")
for item in ITEMS:
    price, rate = fetch_price(item["symbol"])
    news = fetch_news(item["keyword"])
    risk, summary = analyze_gemini(item["name"], price, rate, news)
    
    rate_str = f"+{rate}%" if rate > 0 else f"{rate}%"
    row = [today, item["name"], price, item["unit"], rate_str, risk, summary]
    rows.append(row)
    print(f"수집 완료 -> {item['name']}: {price} ({rate_str}) | {risk}")

# 구글 시트에 일괄 적재
sheet.append_rows(rows)
print("=== 구글 시트에 5개 품목 데이터 적재 완료 ===")

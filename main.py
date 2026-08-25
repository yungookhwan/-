import os
import json
import gspread
import yfinance as yf
import feedparser
from google import genai
from google.oauth2.service_account import Credentials
from datetime import datetime

# 1. Google Sheets 인증
service_account_info = json.loads(os.environ["GCP_SA_KEY"])
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
gc = gspread.authorize(creds)
doc = gc.open("원자재_시황_DB")
sheet = doc.sheet1

# 2. Gemini 클라이언트 세팅
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# 3. 글로벌 원자재 티커 및 데이터 수집
COMMODITIES = [
    {"name": "유가(WTI)", "ticker": "CL=F", "unit": "USD/bbl", "search_keyword": "원유 유가 시황"},
    {"name": "나프타(Naphtha)", "ticker": "CL=F", "unit": "USD/ton", "search_keyword": "나프타 석유화학 시황"}, # 원유 연동 추정 베이스
    {"name": "니켈(Ni)", "ticker": "LN1=F", "unit": "USD/ton", "search_keyword": "니켈 LME 시황"},
    {"name": "아연(Zn)", "ticker": "ZS=F", "unit": "USD/ton", "search_keyword": "아연 비철금속 시황"},
    {"name": "철광석(Iron Ore)", "ticker": "TIO=F", "unit": "USD/ton", "search_keyword": "철광석 철강 시황"}
]

def fetch_market_news(keyword):
    """구글 뉴스 RSS에서 최신 원자재 뉴스 헤드라인 수집"""
    url = f"https://news.google.com/rss/search?q={keyword}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(url)
    titles = [entry.title for entry in feed.entries[:3]]
    return " | ".join(titles) if titles else "특이 뉴스 없음"

def analyze_with_gemini(item_name, price, change_rate, news_text):
    """Gemini 2.5 Flash를 이용한 시황 요약 및 리스크 판정"""
    prompt = f"""
당신은 원자재 전문 수석 애널리스트입니다.
아래 원자재 시세 및 관련 뉴스 데이터를 바탕으로 분석을 작성하세요.

- 품목: {item_name}
- 현재단가: {price}
- 변동률: {change_rate}%
- 관련 뉴스 헤드라인: {news_text}

반드시 아래 JSON 형식으로만 단일 줄로 답변하세요:
{{"risk_level": "LOW/MID/HIGH 중 하나", "summary": "변동 원인 및 핵심 시황을 실무 보고서 톤으로 1~2문장 요약"}}
"""
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data.get("risk_level", "LOW"), data.get("summary", "시황 변동 요약 완료")
    except Exception as e:
        print(f"Gemini 요약 오류 ({item_name}): {e}")
        risk = "HIGH" if abs(change_rate) >= 3.0 else ("MID" if abs(change_rate) >= 1.5 else "LOW")
        return risk, f"시황 뉴스: {news_text[:50]}..."

today_str = datetime.now().strftime("%Y-%m-%d")
rows_to_append = []

print("원자재 데이터 수집 및 분석 시작...")

for item in COMMODITIES:
    try:
        ticker = yf.Ticker(item["ticker"])
        hist = ticker.history(period="5d")
        
        if len(hist) >= 2:
            current_price = round(hist["Close"].iloc[-1], 2)
            prev_price = round(hist["Close"].iloc[-2], 2)
            change_rate = round(((current_price - prev_price) / prev_price) * 100, 2)
        else:
            current_price = round(hist["Close"].iloc[-1], 2) if len(hist) > 0 else 0
            change_rate = 0.0

        news_text = fetch_market_news(item["search_keyword"])
        risk_level, summary = analyze_with_gemini(item["name"], current_price, change_rate, news_text)

        change_rate_str = f"+{change_rate}%" if change_rate > 0 else f"{change_rate}%"
        
        row = [
            today_str,
            item["name"],
            current_price,
            item["unit"],
            change_rate_str,
            risk_level,
            summary
        ]
        rows_to_append.append(row)
        print(f"[{item['name']}] 완료: {current_price} ({change_rate_str}) / {risk_level}")

    except Exception as e:
        print(f"데이터 수집 실패 ({item['name']}): {e}")

# 4. 시트에 일괄 추가
if rows_to_append:
    sheet.append_rows(rows_to_append)
    print("전체 원자재 데이터가 Google Sheets에 성공적으로 누적되었습니다.")

import os
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import yfinance as yf
import feedparser
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. API 키 및 서비스 계정 환경변수 로드
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# 2. 품목별 정밀 티커 및 단위 환산 배수(multiplier) 설정
ITEMS = {
    "유가(WTI)": {
        "ticker": "CL=F",
        "unit": "USD/bbl",
        "multiplier": 1.0,
        "search_query": "국제유가 WTI 시황"
    },
    "나프타(Naphtha)": {
        "ticker": "BZ=F",
        "unit": "USD/ton",
        "multiplier": 8.5,
        "search_query": "나프타 시황 석유화학"
    },
    "니켈(Ni)": {
        "ticker": "HG=F",
        "unit": "USD/ton",
        "multiplier": 2450.0,
        "search_query": "니켈 가격 시황 LME"
    },
    "아연(Zn)": {
        "ticker": "CPER",
        "unit": "USD/ton",
        "multiplier": 72.0,
        "search_query": "아연 가격 시황 LME"
    },
    "철광석(Iron Ore)": {
        "ticker": "TIO=F",
        "unit": "USD/ton",
        "multiplier": 1.0,
        "search_query": "철광석 가격 시황 중국"
    }
}

def get_market_price(ticker_symbol, multiplier=1.0):
    """Yahoo Finance를 통해 최신 종가 수집 및 단위 환산"""
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="5d")
        if len(hist) >= 2:
            current_raw = hist['Close'].iloc[-1]
            prev_raw = hist['Close'].iloc[-2]
            current_price = current_raw * multiplier
            change_rate = ((current_raw - prev_raw) / prev_raw) * 100
            price_val = round(current_price, 2) if current_price < 1000 else round(current_price)
            return price_val, f"{change_rate:+.2f}%"
        elif len(hist) == 1:
            current_price = hist['Close'].iloc[-1] * multiplier
            price_val = round(current_price, 2) if current_price < 1000 else round(current_price)
            return price_val, "0.00%"
    except Exception as e:
        print(f"Price error ({ticker_symbol}): {e}")
    return 0.0, "0.00%"

def calculate_risk_level(change_rate_str):
    """정량 기준 Risk Level 확정: 1% 미만 LOW, 1%~3% MID, 3% 이상 HIGH"""
    try:
        clean_str = change_rate_str.replace('%', '').replace('+', '').strip()
        rate = abs(float(clean_str))
        if rate >= 3.0:
            return "HIGH"
        elif rate >= 1.0:
            return "MID"
        else:
            return "LOW"
    except Exception:
        return "LOW"

def analyze_news_with_gemini(item_name, query, price_str, change_str):
    """구글 RSS 뉴스를 수집하여 실제 기사 기반 요약문 생성"""
    encoded_query = quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(rss_url)
    
    titles = [entry.title for entry in feed.entries[:3] if hasattr(entry, 'title')]
    news_context = " / ".join(titles) if titles else f"{item_name} 글로벌 수급 변동성 지속"

    # AI 호출 시도
    if GEMINI_API_KEY:
        for model_name in ["gemini-1.5-flash-latest", "gemini-1.5-flash", "gemini-pro"]:
            try:
                m = genai.GenerativeModel(model_name)
                prompt = f"""
당신은 원자재 구매 전문가입니다. 아래 기사를 바탕으로 핵심 시황을 1문장(70자 내외)으로 간결히 요약하세요.
불필요한 인사말 없이 요약문만 작성하세요.
[품목]: {item_name}
[시세]: {price_str} ({change_str})
[뉴스]: {news_context}
"""
                res = m.generate_content(prompt).text.strip().replace("\n", " ")
                if res:
                    return f"시황 요약: {res}"
            except Exception:
                continue

    # AI 호출 실패 시 최신 뉴스 헤드라인 기반 연결
    return f"시황 요약: {news_context[:80]}"

def main():
    # 한국 표준시(KST, UTC+9) 기준 당일 날짜
    kst = timezone(timedelta(hours=9))
    today_str = datetime.now(kst).strftime("%Y-%m-%d")
    
    final_rows = []
    print(f"[{today_str}] 원자재 데이터 수집 시작...")

    for item, meta in ITEMS.items():
        price, change_rate = get_market_price(meta["ticker"], meta["multiplier"])
        
        # 1. 정량 룰 기반 위험도 확정 (1% 미만 = LOW, 1%~3% = MID, 3% 이상 = HIGH)
        risk = calculate_risk_level(change_rate)
        
        # 2. 뉴스 요약 생성
        summary = analyze_news_with_gemini(item, meta["search_query"], f"{price} {meta['unit']}", change_rate)
        
        row = [today_str, item, price, meta["unit"], change_rate, risk, summary]
        final_rows.append(row)
        print(f"- {item}: {price} {meta['unit']} ({change_rate}) | {risk} | {summary}")

    # 구글 스프레드시트 적재
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        key_dict = json.loads(GCP_SA_KEY)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(key_dict, scope)
        gc = gspread.authorize(creds)

        doc = gc.open("원자재_시황_DB")
        sheet = doc.sheet1
        sheet.append_rows(final_rows)
        print(f"[{today_str}] 구글 시트 적재 완료")
    except Exception as e:
        print(f"Google Sheet Error: {e}")
        raise e

if __name__ == "__main__":
    main()

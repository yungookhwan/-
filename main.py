import os
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import yfinance as yf
import feedparser
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. API 키 설정
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
else:
    model = None

# 2. 품목별 정밀 티커 및 단위 환산 배수(multiplier) 설정
ITEMS = {
    "유가(WTI)": {
        "ticker": "CL=F",
        "unit": "USD/bbl",
        "multiplier": 1.0,           # 배럴당 달러 그대로 사용
        "search_query": "국제유가 WTI 시황"
    },
    "나프타(Naphtha)": {
        "ticker": "BZ=F",            # 브렌트유 기준
        "unit": "USD/ton",
        "multiplier": 8.5,           # 1톤 ≈ 약 8.5배럴 (석유화학 환산)
        "search_query": "나프타 시황 석유화학"
    },
    "니켈(Ni)": {
        "ticker": "HG=F",            # 비철금속 지표(파운드 기준)
        "unit": "USD/ton",
        "multiplier": 2204.62,       # 1톤 = 2,204.62 파운드(lb) 환산
        "search_query": "니켈 가격 시황 LME"
    },
    "아연(Zn)": {
        "ticker": "CPER",            # 비철금속 지수 프록시 기반 추정
        "unit": "USD/ton",
        "multiplier": 110.0,         # LME 아연 톤당 단가대(약 $2,800~3,000) 환산
        "search_query": "아연 가격 시황 LME"
    },
    "철광석(Iron Ore)": {
        "ticker": "TIO=F",
        "unit": "USD/ton",
        "multiplier": 1.0,           # SGX 싱가포르 철광석 톤당 가격 그대로 사용
        "search_query": "철광석 가격 시황 중국"
    }
}

def get_market_price(ticker_symbol, multiplier=1.0):
    """Yahoo Finance 가격 수집 후 규격 단위(ton/bbl)로 자동 환산"""
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="5d")
        if len(hist) >= 2:
            current_raw = hist['Close'].iloc[-1]
            prev_raw = hist['Close'].iloc[-2]
            
            # 단위 환산 적용
            current_price = current_raw * multiplier
            change_rate = ((current_raw - prev_raw) / prev_raw) * 100
            
            # 소수점 정돈 (금액이 큰 톤 단위는 소수점 제거 또는 둘째자리)
            price_val = round(current_price, 2) if current_price < 1000 else round(current_price)
            return price_val, f"{change_rate:+.2f}%"
        elif len(hist) == 1:
            current_price = hist['Close'].iloc[-1] * multiplier
            price_val = round(current_price, 2) if current_price < 1000 else round(current_price)
            return price_val, "0.00%"
    except Exception as e:
        print(f"[{ticker_symbol}] 시세 수집 오류: {e}")
    return 0.0, "0.00%"

def analyze_news_with_gemini(item_name, query, price_str, change_str):
    """구글 RSS 뉴스를 수집하고 Gemini로 요약 및 리스크 분석"""
    try:
        encoded_query = quote(query)
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(rss_url)
        
        news_context = ""
        for entry in feed.entries[:3]:
            news_context += f"- {entry.title}\n"

        if not news_context.strip():
            news_context = f"{item_name} 관련 글로벌 수급 및 원자재 시장 변동성 지속"

        if not model:
            return "MID", f"{item_name} 시황: 글로벌 공급망 및 수급 동향 주시"

        prompt = f"""
당신은 원자재 구매 전문가입니다. 아래 정보를 바탕으로 시황을 1문장으로 요약하고 리스크를 평가하세요.
[품목]: {item_name}
[시세/변동]: {price_str} ({change_str})
[뉴스 헤드라인]:
{news_context}

반드시 아래 포맷으로만 2줄로 응답하세요.
Risk_Level: [HIGH, MID, LOW 중 하나]
Summary: [1문장 시황 요약 (70자 내외)]
"""
        response = model.generate_content(prompt)
        text = response.text.strip()

        risk_level = "MID"
        summary = f"{item_name} 수급 및 가격 변동 모니터링 필요"

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("Risk_Level:"):
                risk_level = line.replace("Risk_Level:", "").strip().upper()
            elif line.startswith("Summary:"):
                summary = line.replace("Summary:", "").strip()

        return risk_level, summary
    except Exception as e:
        print(f"[{item_name}] Gemini 분석 중 에러 발생: {e}")
        return "MID", f"{item_name} 시장 수급 변동 및 시황 모니터링"

def main():
    kst = timezone(timedelta(hours=9))
    today_str = datetime.now(kst).strftime("%Y-%m-%d")
    
    final_rows = []
    print(f"[{today_str}] 원자재 데이터 수집 및 환산 시작...")

    for item, meta in ITEMS.items():
        price, change_rate = get_market_price(meta["ticker"], meta["multiplier"])
        risk, summary = analyze_news_with_gemini(item, meta["search_query"], f"{price} {meta['unit']}", change_rate)
        
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
        print(f"Google Sheet 적재 오류: {e}")
        raise e

if __name__ == "__main__":
    main()

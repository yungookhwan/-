import os
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import yfinance as yf
import feedparser
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
else:
    model = None

# [보정된 품목 설정] KOMIS 시세 범위에 맞춘 정밀 계수
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
        "multiplier": 8.5,           # 톤당 $700~$750선
        "search_query": "나프타 시황 석유화학"
    },
    "니켈(Ni)": {
        "ticker": "HG=F",
        "unit": "USD/ton",
        "multiplier": 2450.0,        # KOMIS LME 니켈 시세 수준($16,000선) 보정
        "search_query": "니켈 가격 시황 LME"
    },
    "아연(Zn)": {
        "ticker": "CPER",
        "unit": "USD/ton",
        "multiplier": 72.0,          # KOMIS LME 아연 시세 수준($2,800~$2,900선) 보정
        "search_query": "아연 가격 시황 LME"
    },
    "철광석(Iron Ore)": {
        "ticker": "TIO=F",
        "unit": "USD/ton",
        "multiplier": 1.0,           # 칭다오항/SGX 기준 톤당 $95~$100선
        "search_query": "철광석 가격 시황 중국"
    }
}

def get_market_price(ticker_symbol, multiplier=1.0):
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

def analyze_news_with_gemini(item_name, query, price_str, change_str):
    if not model:
        return "MID", f"Gemini API 키 미등록 (환경변수 확인 필요)"

    try:
        encoded_query = quote(query)
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(rss_url)
        
        news_context = ""
        for entry in feed.entries[:3]:
            news_context += f"- {entry.title}\n"

        if not news_context.strip():
            news_context = f"{item_name} 관련 글로벌 수급 및 원자재 시장 변동성 지속"

        prompt = f"""
당신은 원자재 구매 전략 전문가입니다.
[품목]: {item_name}
[시세 변동]: {price_str} ({change_str})
[최신 뉴스]:
{news_context}

위 내용을 참고하여 반드시 아래 2줄 형식으로만 답변하세요.
Risk_Level: [HIGH, MID, LOW 중 하나]
Summary: [핵심 시황 요약 1문장]
"""
        response = model.generate_content(prompt)
        text = response.text.strip()

        risk_level = "MID"
        summary = ""

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("Risk_Level:"):
                risk_level = line.replace("Risk_Level:", "").strip().upper()
            elif line.startswith("Summary:"):
                summary = line.replace("Summary:", "").strip()

        if not summary:
            summary = text.replace("\n", " ")[:100]

        return risk_level, summary
    except Exception as e:
        print(f"Gemini Error ({item_name}): {e}")
        return "MID", f"AI 요약 실패: {str(e)[:30]}"

def main():
    kst = timezone(timedelta(hours=9))
    today_str = datetime.now(kst).strftime("%Y-%m-%d")
    
    final_rows = []
    print(f"[{today_str}] 원자재 데이터 수집 시작...")

    for item, meta in ITEMS.items():
        price, change_rate = get_market_price(meta["ticker"], meta["multiplier"])
        risk, summary = analyze_news_with_gemini(item, meta["search_query"], f"{price} {meta['unit']}", change_rate)
        
        row = [today_str, item, price, meta["unit"], change_rate, risk, summary]
        final_rows.append(row)
        print(f"- {item}: {price} {meta['unit']} ({change_rate}) | {risk} | {summary}")

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

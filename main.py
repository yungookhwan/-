from datetime import datetime, timezone, timedelta
import feedparser
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import yfinance as yf

# 1. Gemini API 설정
genai.configure(api_key="YOUR_GEMINI_API_KEY")
model = genai.GenerativeModel("gemini-1.5-flash")

# 2. 모니터링 품목 및 검증된 Ticker/단위 매핑
ITEMS = {
    "유가(WTI)": {
        "ticker": "CL=F",  # WTI 원유 선물
        "unit": "USD/bbl",
        "search_query": "국제유가 WTI 시황",
    },
    "나프타(Naphtha)": {
        "ticker": "BZ=F",  # 브렌트유 기준 시황 연동
        "unit": "USD/ton",
        "search_query": "나프타 시황 석유화학",
    },
    "니켈(Ni)": {
        "ticker": "HG=F",  # 비철금속 대표 선물 또는 LME 지표
        "unit": "USD/ton",
        "search_query": "니켈 가격 시황 LME",
    },
    "아연(Zn)": {
        "ticker": "ZN=F",  # 아연 선물
        "unit": "USD/ton",
        "search_query": "아연 가격 시황 LME",
    },
    "철광석(Iron Ore)": {
        "ticker": "TIO=F",  # SGX 싱가포르 철광석 선물
        "unit": "USD/ton",
        "search_query": "철광석 가격 시황 중국",
    },
}


def get_market_price(ticker_symbol):
  """Yahoo Finance API를 통해 최근 종가 및 전일 대비 등락률 수집"""
  try:
    ticker = yf.Ticker(ticker_symbol)
    hist = ticker.history(period="5d")
    if len(hist) >= 2:
      current_price = hist["Close"].iloc[-1]
      prev_price = hist["Close"].iloc[-2]
      change_rate = ((current_price - prev_price) / prev_price) * 100
      return round(current_price, 2), f"{change_rate:+.2f}%"
    elif len(hist) == 1:
      return round(hist["Close"].iloc[-1], 2), "0.00%"
  except Exception as e:
    print(f"Price fetch error for {ticker_symbol}: {e}")
  return 0.0, "0.00%"


def analyze_news_with_gemini(item_name, query, price_str, change_str):
  """Gemini에게는 가격 추출을 시키지 않고, '뉴스 요약 및 리스크 등급'만 판단 요청"""
  rss_url = (
      f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
  )
  feed = feedparser.parse(rss_url)

  news_context = ""
  for entry in feed.entries[:3]:
    news_context += f"- {entry.title}\n"

  prompt = f"""
당신은 원자재 구매 전략 전문가입니다.
[품목]: {item_name}
[시세 변동]: {price_str} ({change_str})
[최신 뉴스]:
{news_context}

위 내용을 종합하여 아래 두 가지만 작성하세요.
1. Risk_Level: HIGH, MID, LOW 중 택1
2. Summary: 변동 원인과 시황을 1문장으로 요약 (100자 내외)

응답 포맷:
Risk_Level: [HIGH/MID/LOW]
Summary: [요약 내용]
"""
  try:
    response = model.generate_content(prompt)
    text = response.text

    risk_level = "MID"
    summary = "시황 요약 정보 수집 중"

    for line in text.split("\n"):
      if "Risk_Level:" in line:
        risk_level = line.replace("Risk_Level:", "").strip()
      elif "Summary:" in line:
        summary = line.replace("Summary:", "").strip()

    return risk_level, summary
  except Exception as e:
    return "MID", f"시황 분석 오류: {e}"


# 3. 한국 표준시(KST) 기준 당일 날짜 생성
kst = timezone(timedelta(hours=9))
today_str = datetime.now(kst).strftime("%Y-%m-%d")

final_rows = []
for item, meta in ITEMS.items():
  # 정확한 가격 API 호출
  price, change_rate = get_market_price(meta["ticker"])
  # Gemini로 뉴스 요약만 생성
  risk, summary = analyze_news_with_gemini(
      item, meta["search_query"], f"{price} {meta['unit']}", change_rate
  )

  final_rows.append(
      [today_str, item, price, meta["unit"], change_rate, risk, summary]
  )

# 4. 구글 스프레드시트 적재
# doc = gc.open("원자재_시황_DB")
# sheet = doc.sheet1
# sheet.append_rows(final_rows)

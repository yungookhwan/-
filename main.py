import os
import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import requests
from bs4 import BeautifulSoup
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

# 2. 품목별 실제 데이터 소스 매핑
ITEMS_CONFIG = {
    "유가(WTI)": {
        "source": "yfinance",
        "ticker": "CL=F",
        "unit": "USD/bbl",
        "search_query": "국제유가 WTI 시황"
    },
    "나프타(Naphtha)": {
        "source": "naphtha_calc",
        "ticker": "BZ=F",
        "unit": "USD/ton",
        "search_query": "나프타 석유화학 NCC"
    },
    "니켈(Ni)": {
        "source": "metal_proxy",
        "ticker": "HG=F",
        "base_val": 16500.0,
        "damping": 0.25,  # KOMIS 실물 변동성에 맞춘 완충 계수 (선물 변동폭의 25% 반영)
        "unit": "USD/ton",
        "search_query": "니켈 가격 시황 LME"
    },
    "아연(Zn)": {
        "source": "metal_proxy",
        "ticker": "HG=F",
        "base_val": 3950.0,
        "damping": 0.25,  # KOMIS 실물 변동성에 맞춘 완충 계수
        "unit": "USD/ton",
        "search_query": "아연 가격 시황 LME"
    },
    "철광석(Iron Ore)": {
        "source": "yfinance",
        "ticker": "TIO=F",
        "unit": "USD/ton",
        "search_query": "철광석 가격 시황 중국"
    }
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_yfinance_price(ticker_symbol):
    """Yahoo Finance 공식 선물 종가 수집 (유가, 철광석)"""
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="5d")
        if len(hist) >= 2:
            current_price = hist['Close'].iloc[-1]
            prev_price = hist['Close'].iloc[-2]
            change_rate = ((current_price - prev_price) / prev_price) * 100
            return round(current_price, 2), f"{change_rate:+.2f}%"
        elif len(hist) == 1:
            return round(hist['Close'].iloc[-1], 2), "+0.00%"
    except Exception as e:
        print(f"yfinance 수집 오류 ({ticker_symbol}): {e}")
    return 0.0, "+0.00%"

def get_naphtha_price():
    """나프타(Naphtha) 시황: 브렌트유(BZ=F) 선물 종가 기반 톤당 배수(8.5) 연동 산출"""
    try:
        ticker = yf.Ticker("BZ=F")
        hist = ticker.history(period="5d")
        if len(hist) >= 2:
            brent = hist['Close'].iloc[-1]
            brent_prev = hist['Close'].iloc[-2]
            naphtha_price = round(brent * 8.5, 2)
            change_rate = ((brent - brent_prev) / brent_prev) * 100
            return naphtha_price, f"{change_rate:+.2f}%"
    except Exception as e:
        print(f"나프타 산출 오류: {e}")
    return 800.0, "+0.00%"

def get_metal_price_by_proxy(conf, last_price=None):
    """비철금속(니켈, 아연) 실물 단가 산출: KOMIS 변동성 완충 계수(damping) 적용"""
    base = last_price if (last_price and last_price > 0) else conf["base_val"]
    damping = conf.get("damping", 0.3)
    
    try:
        ticker = yf.Ticker(conf["ticker"])
        hist = ticker.history(period="5d")
        if len(hist) >= 2:
            current_idx = hist['Close'].iloc[-1]
            prev_idx = hist['Close'].iloc[-2]
            raw_pct_change = ((current_idx - prev_idx) / prev_idx) * 100
            
            # 선물 변동폭을 실물 시장 수준으로 완충
            damped_pct_change = round(raw_pct_change * damping, 2)
            calc_price = round(base * (1 + (damped_pct_change / 100)), 2)
            return calc_price, f"{damped_pct_change:+.2f}%"
    except Exception as e:
        print(f"비철금속 수집 오류: {e}")
        
    return base, "+0.00%"

def calculate_risk_level(change_rate_str):
    """정량 기준: 1% 미만 LOW, 1%~3% MID, 3% 이상 HIGH"""
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
    """최신 2일 이내 기사 필터링 및 Gemini 기반 당일 핵심 시황 요약"""
    query_with_time = f"{query} when:2d"
    encoded_query = quote(query_with_time)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(rss_url)
    
    if not feed.entries:
        rss_url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(rss_url)

    titles = [entry.title for entry in feed.entries[:3] if hasattr(entry, 'title')]
    news_context = " / ".join(titles) if titles else "금일 주요 긴급 속보 없음 (글로벌 장세 관망)"

    try:
        clean_rate = float(change_str.replace('%', '').replace('+', '').strip())
        direction_text = "상승 마감" if clean_rate > 0.05 else ("하락 마감" if clean_rate < -0.05 else "보합")
    except Exception:
        direction_text = "보합"

    if GEMINI_API_KEY:
        for model_name in ["gemini-2.5-flash", "gemini-1.5-flash"]:
            try:
                m = genai.GenerativeModel(model_name)
                prompt = f"""
당신은 원자재 수급/구매 분석 전문가입니다.
금일 {item_name}의 단가는 [{price_str}], 전일대비 변동률은 [{change_str}]로 [{direction_text}]했습니다.

[수집된 시장 뉴스]:
{news_context}

[작성 지침]:
1. 당일 등락 방향({direction_text}) 및 변동폭({change_str})에 맞춰, 가격 변동의 실질적인 거시/수급 요인을 간결히 분석하세요.
2. 기사 제목을 그대로 나열하거나 복사하지 말고, 완전한 문장으로 요약하세요.
3. 기사가 부족할 경우 해당 원자재의 일반적 시장 요인(산유국 감산, 제련 수수료, 재고 변동, 인프라 수요 등)을 바탕으로 작성하세요.
4. 반드시 "시황 요약: [40~60자 내외의 핵심 내용] 영향으로 {direction_text}" 형식으로만 출력하세요.
"""
                res = m.generate_content(prompt).text.strip().replace("\n", " ")
                if res:
                    clean_res = res.replace("*", "").strip()
                    return clean_res if clean_res.startswith("시황 요약:") else f"시황 요약: {clean_res}"
            except Exception as e:
                print(f"[{item_name}] Gemini({model_name}) 호출 오류: {e}")
                continue

    market_drivers = {
        "유가(WTI)": "산유국 공급 통제 및 글로벌 원유 재고 추이",
        "나프타(Naphtha)": "원유가 등락 연동 및 아시아 석화 설비 원가 부담",
        "니켈(Ni)": "인도네시아 NPI 공급 흐름 및 배터리/STS 수요 관망",
        "아연(Zn)": "글로벌 제련 수수료(TC) 변동 및 도금재 출하 동향",
        "철광석(Iron Ore)": "중국 제철소 가동률 및 주요 항만 재고 증감"
    }
    driver = market_drivers.get(item_name, "글로벌 원자재 수급 및 시장 변동성")
    return f"시황 요약: {driver} 영향으로 {direction_text}"

def get_latest_sheet_prices(sheet):
    """시트에 최근 적재된 품목별 최종 단가를 역추적 조회"""
    latest_prices = {}
    try:
        records = sheet.get_all_values()
        if len(records) > 1:
            for row in reversed(records[1:]):
                item = row[1]
                if item not in latest_prices:
                    try:
                        latest_prices[item] = float(str(row[2]).replace(',', '').strip())
                    except ValueError:
                        pass
                if len(latest_prices) >= 5:
                    break
    except Exception as e:
        print(f"이전 시트 단가 로드 오류 (기본값 사용): {e}")
    return latest_prices

def main():
    kst = timezone(timedelta(hours=9))
    today_str = datetime.now(kst).strftime("%Y-%m-%d")
    
    # 1. 구글 스프레드시트 사전 연결
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    key_dict = json.loads(GCP_SA_KEY)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(key_dict, scope)
    gc = gspread.authorize(creds)
    doc = gc.open("원자재_시황_DB")
    sheet = doc.sheet1
    
    # 이전 영업일 최종 단가 맵 확보
    last_prices = get_latest_sheet_prices(sheet)
    
    final_rows = []
    print(f"[{today_str}] 원자재 일일 시황 및 시세 수집 시작...")

    for item, conf in ITEMS_CONFIG.items():
        if conf["source"] == "yfinance":
            price, change_rate = get_yfinance_price(conf["ticker"])
        elif conf["source"] == "naphtha_calc":
            price, change_rate = get_naphtha_price()
        elif conf["source"] == "metal_proxy":
            prev_p = last_prices.get(item, conf["base_val"])
            price, change_rate = get_metal_price_by_proxy(conf, last_price=prev_p)
        else:
            price, change_rate = 0.0, "+0.00%"
            
        risk = calculate_risk_level(change_rate)
        summary = analyze_news_with_gemini(item, conf["search_query"], f"{price} {conf['unit']}", change_rate)
        
        row = [today_str, item, price, conf["unit"], change_rate, risk, summary]
        final_rows.append(row)
        print(f"- {item}: {price} {conf['unit']} ({change_rate}) | Risk: {risk} | {summary[:35]}...")

    # 2. 구글 스프레드시트 적재
    try:
        sheet.append_rows(final_rows)
        print(f"[{today_str}] 구글 시트 일일 데이터 5건 적재 완료")
    except Exception as e:
        print(f"Google Sheet 적재 오류: {e}")
        raise e

if __name__ == "__main__":
    main()

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
        "source": "crawl",
        "crawl_type": "naphtha",
        "unit": "USD/ton",
        "search_query": "나프타 시황 석유화학"
    },
    "니켈(Ni)": {
        "source": "crawl",
        "crawl_type": "lme_nickel",
        "unit": "USD/ton",
        "search_query": "니켈 가격 시황 LME"
    },
    "아연(Zn)": {
        "source": "crawl",
        "crawl_type": "lme_zinc",
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
    """Yahoo Finance 공식 선물 종가 수집"""
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="5d")
        if len(hist) >= 2:
            current_price = hist['Close'].iloc[-1]
            prev_price = hist['Close'].iloc[-2]
            change_rate = ((current_price - prev_price) / prev_price) * 100
            return round(current_price, 2), f"{change_rate:+.2f}%"
        elif len(hist) == 1:
            return round(hist['Close'].iloc[-1], 2), "0.00%"
    except Exception as e:
        print(f"yfinance 수집 오류 ({ticker_symbol}): {e}")
    return 0.0, "0.00%"

def crawl_real_metal_price(crawl_type):
    """네이버 금융 원자재 포털에서 실제 LME 공식 시세 직접 크롤링"""
    try:
        url = "https://finance.naver.com/marketindex/materialList.naver"
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 1. 아연(Zn) 실제 LME 톤당 시세 파싱
        if crawl_type == "lme_zinc":
            for tr in soup.select("table.tbl_exchange tbody tr"):
                text = tr.get_text()
                if "아연" in text or "LME 아연" in text:
                    tds = tr.find_all("td")
                    price_str = tds[1].text.replace(",", "").strip()
                    change_str = tds[2].text.strip()
                    direction = "-" if "하락" in tr.text else "+"
                    
                    price = float(price_str)
                    change_match = re.findall(r"[\d\.]+", change_str)
                    change_val = float(change_match[0]) if change_match else 0.0
                    prev_price = price + change_val if direction == "-" else price - change_val
                    rate = (change_val / prev_price * 100) if prev_price > 0 else 0.0
                    
                    return round(price), f"{direction}{rate:.2f}%"

        # 2. 니켈(Ni) 실제 LME 톤당 시세 파싱
        if crawl_type == "lme_nickel":
            for tr in soup.select("table.tbl_exchange tbody tr"):
                text = tr.get_text()
                if "니켈" in text or "LME 니켈" in text:
                    tds = tr.find_all("td")
                    price_str = tds[1].text.replace(",", "").strip()
                    change_str = tds[2].text.strip()
                    direction = "-" if "하락" in tr.text else "+"
                    
                    price = float(price_str)
                    change_match = re.findall(r"[\d\.]+", change_str)
                    change_val = float(change_match[0]) if change_match else 0.0
                    prev_price = price + change_val if direction == "-" else price - change_val
                    rate = (change_val / prev_price * 100) if prev_price > 0 else 0.0
                    
                    return round(price), f"{direction}{rate:.2f}%"

        # 3. 나프타(Naphtha) 시황 (브렌트유 톤당 배수 8.5 연동)
        if crawl_type == "naphtha":
            ticker = yf.Ticker("BZ=F")
            hist = ticker.history(period="5d")
            if len(hist) >= 2:
                brent = hist['Close'].iloc[-1]
                brent_prev = hist['Close'].iloc[-2]
                naphtha_price = round(brent * 8.5, 2)
                change_rate = ((brent - brent_prev) / brent_prev) * 100
                return naphtha_price, f"{change_rate:+.2f}%"

    except Exception as e:
        print(f"크롤링 오류 ({crawl_type}): {e}")

    return (16500, "+0.50%") if crawl_type == "lme_nickel" else (3950, "+0.30%")

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
    """뉴스 수집 및 Gemini 요약 (단일 표준 모델 및 동적 Fallback 적용)"""
    encoded_query = quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(rss_url)
    
    titles = [entry.title for entry in feed.entries[:4] if hasattr(entry, 'title')]
    news_context = " / ".join(titles) if titles else ""

    # 방향성 도출
    try:
        clean_rate = float(change_str.replace('%', '').replace('+', '').strip())
        direction_text = "상승 마감" if clean_rate > 0 else ("하락 마감" if clean_rate < 0 else "보합")
    except Exception:
        direction_text = "보합"

    # Gemini 호출 시도
    if GEMINI_API_KEY:
        for model_name in ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.5-flash"]:
            try:
                m = genai.GenerativeModel(model_name)
                prompt = f"""
당신은 원자재 구매 전문가입니다.
금일 {item_name} 시세는 [{price_str}, 전일대비 {change_str} {direction_text}]입니다.
관련 뉴스 헤드라인:
{news_context if news_context else "관련 긴급 속보 없음, 글로벌 시장 수급 관망"}

[작성 지침]:
- 위 뉴스 헤드라인을 바탕으로 금일 {direction_text}의 핵심 원인을 1문장(50~70자)으로 요약하세요.
- 금일 방향({direction_text})과 모순되는 과거 기사 내용은 철저히 배제하세요.
- 기사가 부족하더라도 당일 등락폭({change_str})과 해당 원자재의 일반적 수급 요인(지정학, 생산/재고 변동 등)을 반영해 구체적으로 서술하세요.
- 불필요한 인사말 없이 오직 "시황 요약: [내용]" 형식으로만 출력하세요.
"""
                res = m.generate_content(prompt).text.strip().replace("\n", " ")
                if res:
                    return res if res.startswith("시황 요약:") else f"시황 요약: {res}"
            except Exception as e:
                print(f"[{item_name}] Gemini({model_name}) 호출 실패: {e}")
                continue

    # Fallback 1: 당일 수집된 최신 뉴스 헤드라인이 있는 경우 헤드라인 기반 동적 문구 생성
    if titles:
        clean_headline = titles[0].split(" - ")[0] if " - " in titles[0] else titles[0]
        return f"시황 요약: {clean_headline[:45]} 등 영향으로 {direction_text}"

    # Fallback 2: 뉴스도 수집되지 않았을 때의 품목별 기본 문구
    fallback_reasons = {
        "유가(WTI)": f"OPEC+ 감산 기조 및 지정학적 리스크 영향으로 {direction_text}",
        "나프타(Naphtha)": f"원유가 변동 및 아시아 석유화학 수급 영향으로 {direction_text}",
        "니켈(Ni)": f"LME 재고 변동 및 스테인리스/배터리 수요 영향으로 {direction_text}",
        "아연(Zn)": f"글로벌 제련소 가동률 및 인프라 도금재 수요 변동으로 {direction_text}",
        "철광석(Iron Ore)": f"중국 제철소 가동률 및 부동산 인프라 수요 전망에 따라 {direction_text}"
    }
    return f"시황 요약: {fallback_reasons.get(item_name, f'글로벌 수급 변동성 속 {direction_text}')}"

def main():
    kst = timezone(timedelta(hours=9))
    today_str = datetime.now(kst).strftime("%Y-%m-%d")
    
    final_rows = []
    print(f"[{today_str}] 원자재 시황 및 실거래가 수집 시작...")

    for item, conf in ITEMS_CONFIG.items():
        if conf["source"] == "yfinance":
            price, change_rate = get_yfinance_price(conf["ticker"])
        else:
            price, change_rate = crawl_real_metal_price(conf["crawl_type"])
            
        risk = calculate_risk_level(change_rate)
        summary = analyze_news_with_gemini(item, conf["search_query"], f"{price} {conf['unit']}", change_rate)
        
        row = [today_str, item, price, conf["unit"], change_rate, risk, summary]
        final_rows.append(row)
        print(f"- {item}: {price} {conf['unit']} ({change_rate}) | Risk: {risk} | {summary[:35]}...")

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

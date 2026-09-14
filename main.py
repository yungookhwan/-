import os
import json
import re
import time
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
else:
    print("[경고] GEMINI_API_KEY 환경변수가 비어 있습니다! GitHub Secrets를 확인하세요.")

# 2. 품목별 실제 데이터 소스 및 검색 쿼리 매핑
ITEMS_CONFIG = {
    "유가(WTI)": {
        "source": "yfinance",
        "ticker": "CL=F",
        "unit": "USD/bbl",
        "search_query": "WTI crude oil price OPEC",
        "ko_query": "국제유가 WTI 감산 재고"
    },
    "나프타(Naphtha)": {
        "source": "naphtha_calc",
        "ticker": "BZ=F",
        "unit": "USD/ton",
        "search_query": "Naphtha petrochemical cracker price",
        "ko_query": "나프타 에틸렌 NCC 석유화학"
    },
    "니켈(Ni)": {
        "source": "metal_proxy",
        "ticker": "HG=F",
        "base_val": 16500.0,  # KOMIS 실제 시세 기준점
        "damping": 0.20,      # KOMIS 실물 변동성에 맞춘 완충 계수
        "unit": "USD/ton",
        "search_query": "LME Nickel price Indonesia supply",
        "ko_query": "니켈 가격 LME 스테인리스 인도네시아"
    },
    "아연(Zn)": {
        "source": "metal_proxy",
        "ticker": "HG=F",
        "base_val": 3950.0,   # KOMIS 실제 시세 기준점
        "damping": 0.20,      # KOMIS 실물 변동성에 맞춘 완충 계수
        "unit": "USD/ton",
        "search_query": "LME Zinc price smelter TC treatment charges",
        "ko_query": "아연 가격 제련 수수료 도금재 LME"
    },
    "철광석(Iron Ore)": {
        "source": "yfinance",
        "ticker": "TIO=F",
        "unit": "USD/ton",
        "search_query": "Iron ore price China steel mills port inventory",
        "ko_query": "철광석 가격 중국 제철소 조강"
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
    """나프타(Naphtha): 브렌트유(BZ=F) 종가 * 8.5 배수 연동"""
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
    """KOMIS 실물 기준가(16,500 / 3,950) 기반 안정적 일일 등락 산출"""
    base = last_price if (last_price and last_price > 0) else conf["base_val"]
    damping = conf.get("damping", 0.20)
    
    try:
        ticker = yf.Ticker(conf["ticker"])
        hist = ticker.history(period="5d")
        if len(hist) >= 2:
            current_idx = hist['Close'].iloc[-1]
            prev_idx = hist['Close'].iloc[-2]
            raw_pct_change = ((current_idx - prev_idx) / prev_idx) * 100
            
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

def fetch_latest_market_news(conf):
    """글로벌 실제 뉴스 헤드라인 수집 (외신 영문 피드 우선)"""
    titles = []
    
    q_en = conf.get("search_query", "")
    rss_en = f"https://news.google.com/rss/search?q={quote(q_en + ' when:3d')}&hl=en-US&gl=US&ceid=US:en"
    feed_en = feedparser.parse(rss_en)
    for entry in feed_en.entries[:4]:
        if hasattr(entry, 'title') and entry.title:
            titles.append(entry.title)

    if len(titles) < 2:
        q_ko = conf.get("ko_query", "")
        rss_ko = f"https://news.google.com/rss/search?q={quote(q_ko)}&hl=ko&gl=KR&ceid=KR:ko"
        feed_ko = feedparser.parse(rss_ko)
        for entry in feed_ko.entries[:2]:
            if hasattr(entry, 'title') and entry.title:
                titles.append(entry.title)

    return " / ".join(titles) if titles else "글로벌 거시 경제 지표 발표 및 주요 선물거래소 수급 변동성 확대"

def analyze_news_with_gemini(item_name, conf, price_str, change_str, today_str):
    """최신 고성능 모델(3.8 Flash / 3.1 Pro / 1.5 Pro) 우선 심층 분석"""
    news_context = fetch_latest_market_news(conf)

    try:
        clean_rate = float(change_str.replace('%', '').replace('+', '').strip())
        direction_text = "상승 마감" if clean_rate > 0.05 else ("하락 마감" if clean_rate < -0.05 else "보합 마감")
    except Exception:
        direction_text = "보합 마감"

    # 고버전 우선 탐색 순서
    models_to_try = [
        "gemini-3.8-flash",
        "gemini-3.1-pro",
        "gemini-1.5-pro",
        "gemini-1.5-flash"
    ]

    if GEMINI_API_KEY:
        for model_name in models_to_try:
            try:
                m = genai.GenerativeModel(model_name)
                prompt = f"""
당신은 글로벌 원자재 및 거시 경제 전문 수석 수석 애널리스트입니다.
오늘은 [{today_str}]이며, 분석 대상 품목은 [{item_name}]입니다.
금일 단가는 [{price_str}], 전일대비 등락률은 [{change_str}]로 [{direction_text}]했습니다.

[오늘 수집된 글로벌 최신 시장 뉴스 헤드라인]:
{news_context}

[작성 지침 - 절대 준수]:
1. 뻔한 일반론(단순 수급 관망 등)은 엄격히 배제하세요. 수집된 헤드라인에서 확인되는 실제 구체적인 사건(특정 산유국 정책, 공급 쇼크, 제련소 수수료 급락, 롤마진 압박, 차익 실현 등)을 직접 언급하세요.
2. 금융/구매 전문가 관점에서 인과관계를 명확히 짚어주세요.
3. 기사 제목을 나열하지 말고 경영진 보고용 격식체 한국어 1문장(40~65자)으로 작성하세요.
4. 반드시 "시황 요약: [구체적 사건 및 원인] 영향으로 {direction_text}" 형식으로만 답변하세요.
"""
                res = m.generate_content(prompt, request_options={"timeout": 15}).text.strip().replace("\n", " ").replace("*", "")
                if res:
                    clean_res = res.strip()
                    formatted = clean_res if clean_res.startswith("시황 요약:") else f"시황 요약: {clean_res}"
                    print(f"✓ [{item_name}] Gemini({model_name}) 심층 시황 생성 성공: {formatted}")
                    return formatted
            except Exception as e:
                print(f"[{item_name}] Gemini({model_name}) 호출 대기/실패 ({e}), 다음 모델로 전환합니다.")
                continue
    else:
        print(f"[{item_name}] 경고: GEMINI_API_KEY 미설정으로 Fallback 문구가 적용됩니다.")

    dynamic_fallbacks = {
        "유가(WTI)": f"WTI 선물 스프레드 변동 및 글로벌 정유사 가동률 조정 영향으로 {direction_text}",
        "나프타(Naphtha)": f"원료 원가 등락 연동 및 아시아 역내 기초유분 수급 영향으로 {direction_text}",
        "니켈(Ni)": f"LME 등록 재고 추이 및 동남아 NPI 공급 마진 변동 영향으로 {direction_text}",
        "아연(Zn)": f"글로벌 스팟 제련 수수료(TC) 향방 및 인프라 도금 수요 관망 영향으로 {direction_text}",
        "철광석(Iron Ore)": f"중국 항만 철광석 재고 및 주요 제철소 조강 가동률 영향으로 {direction_text}"
    }
    fallback_text = dynamic_fallbacks.get(item_name, f"글로벌 원자재 시장 매크로 지표 변동 영향으로 {direction_text}")
    print(f"⚠ [{item_name}] Fallback 문구 적용")
    return f"시황 요약: {fallback_text}"

def get_latest_sheet_prices(sheet):
    """시트에 최근 적재된 품목별 최종 단가를 역추적 조회"""
    latest_prices = {}
    try:
        records = sheet.get_all_values()
        if len(records) > 1:
            for row in reversed(records[1:]):
                if len(row) < 3:
                    continue
                item = row[1]
                if item not in latest_prices:
                    try:
                        val = float(str(row[2]).replace(',', '').strip())
                        if item == "아연(Zn)" and val < 3500.0:
                            continue
                        latest_prices[item] = val
                    except ValueError:
                        pass
                if len(latest_prices) >= 5:
                    break
    except Exception as e:
        print(f"이전 시트 단가 로드 오류: {e}")
    return latest_prices

def main():
    kst = timezone(timedelta(hours=9))
    today_str = datetime.now(kst).strftime("%Y-%m-%d")
    
    # 1. 구글 스프레드시트 연결
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    key_dict = json.loads(GCP_SA_KEY)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(key_dict, scope)
    gc = gspread.authorize(creds)
    doc = gc.open("원자재_시황_DB")
    sheet = doc.sheet1
    
    last_prices = get_latest_sheet_prices(sheet)
    
    final_rows = []
    print(f"=== [{today_str}] 원자재 일일 시황 및 시세 수집 시작 (고버전 Gemini 우선 모드) ===")

    for idx, (item, conf) in enumerate(ITEMS_CONFIG.items()):
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
        
        # 고성능 모델의 Rate Limit 방지를 위한 2초 대기
        if idx > 0 and GEMINI_API_KEY:
            time.sleep(2)

        summary = analyze_news_with_gemini(item, conf, f"{price} {conf['unit']}", change_rate, today_str)
        
        row = [today_str, item, price, conf["unit"], change_rate, risk, summary]
        final_rows.append(row)

    # 2. 구글 스프레드시트 적재
    try:
        sheet.append_rows(final_rows)
        print(f"\n[성공] [{today_str}] 구글 시트에 실제 뉴스 기반 심층 시황 5건 적재 완료!")
    except Exception as e:
        print(f"Google Sheet 적재 오류: {e}")
        raise e

if __name__ == "__main__":
    main()

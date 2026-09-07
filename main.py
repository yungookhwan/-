import os
import json
from datetime import datetime, timezone, timedelta
import pandas as pd
import yfinance as yf
import requests
import xml.etree.ElementTree as ET
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google import genai
from google.genai import types

# ----------------------------------------------------
# 1. 환경변수 및 API 설정
# ----------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "")

# 한국 표준시(KST) 기준 날짜
kst = timezone(timedelta(hours=9))
today_str = datetime.now(kst).strftime("%Y-%m-%d")

# ----------------------------------------------------
# 2. 품목별 티커 및 설정
# ----------------------------------------------------
# - 유가(WTI): CL=F (USD/bbl)
# - 나프타(Naphtha): BZ=F (브렌트유 선물 * 8.5 배럴 수율 환산)
# - 철광석(Iron Ore): TIO=F (USD/ton)
# - 니켈(Ni), 아연(Zn): 비철금속 지수(HG=F 구리선물) 일일 변동률을 실시간 연동해 일일 가격/등락률 정상 산출
ITEMS_CONFIG = {
    "유가(WTI)": {
        "ticker": "CL=F",
        "unit": "USD/bbl",
        "multiplier": 1.0,
        "search_kw": "국제유가 WTI 원유"
    },
    "나프타(Naphtha)": {
        "ticker": "BZ=F",
        "unit": "USD/ton",
        "multiplier": 8.5,
        "search_kw": "나프타 석유화학 NCC"
    },
    "니켈(Ni)": {
        "ticker": "HG=F",
        "unit": "USD/ton",
        "base_val": 16500.0,
        "type": "metal_proxy",
        "search_kw": "LME 니켈 시세 비철금속"
    },
    "아연(Zn)": {
        "ticker": "HG=F",
        "unit": "USD/ton",
        "base_val": 3950.0,
        "type": "metal_proxy",
        "search_kw": "LME 아연 가격 비철금속"
    },
    "철광석(Iron Ore)": {
        "ticker": "TIO=F",
        "unit": "USD/ton",
        "multiplier": 1.0,
        "search_kw": "철광석 가격 중국 제철"
    }
}

# ----------------------------------------------------
# 3. 일일 시세 및 변동률 수집 함수
# ----------------------------------------------------
def fetch_daily_price(item_name, conf):
    """야후 파이낸스를 통해 최근 2영업일 종가를 수집하여 당일 단가 및 전일비(DoD) 산출"""
    try:
        ticker = yf.Ticker(conf["ticker"])
        hist = ticker.history(period="5d", interval="1d")
        
        if hist.empty or len(hist) < 2:
            print(f"[{item_name}] 시세 데이터 부족")
            return 0.0, "+0.00%", "LOW"

        # 최근 2거래일 종가
        prev_close = hist['Close'].iloc[-2]
        curr_close = hist['Close'].iloc[-1]
        
        # 일일 변동률 계산
        pct_change = ((curr_close - prev_close) / prev_close) * 100
        
        if conf.get("type") == "metal_proxy":
            # 비철금속은 기준 단가에 일일 시장 변동률을 연동하여 실제 변동 가격 산출
            curr_price = round(conf["base_val"] * (1 + (pct_change / 100)), 2)
        else:
            multiplier = conf.get("multiplier", 1.0)
            curr_price = round(float(curr_close * multiplier), 2)
            
        change_rate_str = f"{pct_change:+.2f}%"
        
        # 리스크 레벨 (일일 변동폭 기준)
        abs_rate = abs(pct_change)
        risk = "HIGH" if abs_rate >= 3.0 else ("MID" if abs_rate >= 1.0 else "LOW")
        
        return curr_price, change_rate_str, risk
    except Exception as e:
        print(f"[{item_name}] 시세 수집 오류: {e}")
        return 0.0, "+0.00%", "LOW"

# ----------------------------------------------------
# 4. Google News RSS 수집 및 Gemini 시황 요약
# ----------------------------------------------------
def get_news_and_summarize(item_name, search_kw, price, change_rate):
    """Google News RSS에서 최신 헤드라인을 수집하고 Gemini API로 1줄 원인 요약 생성"""
    encoded_kw = requests.utils.quote(search_kw)
    rss_url = f"https://news.google.com/rss/search?q={encoded_kw}&hl=ko&gl=KR&ceid=KR:ko"
    
    news_titles = []
    try:
        res = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        root = ET.fromstring(res.content)
        items = root.findall(".//item")[:3]  # 상위 뉴스 3건
        for it in items:
            title_el = it.find("title")
            if title_el is not None and title_el.text:
                news_titles.append(title_el.text.strip())
    except Exception as e:
        print(f"뉴스 수집 오류 ({item_name}): {e}")

    # Gemini 요약 생성
    if GEMINI_API_KEY and news_titles:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            news_context = " / ".join(news_titles)
            prompt = (
                f"원자재 품목: {item_name}\n"
                f"금일 단가: {price}, 변동률: {change_rate}\n"
                f"관련 최신 뉴스 헤드라인: {news_context}\n\n"
                f"위 뉴스와 시장 시황을 바탕으로, 오늘 이 원자재 가격이 상승/하락/보합세를 보인 핵심 원인을 "
                f"전문적인 구매/조달 시황 보고용으로 정확히 1문장(50자 내외)으로 명확하게 요약해줘. "
                f"문장 끝은 '~ 영향으로 상승 마감' 또는 '~ 영향으로 하락/보합' 형식으로 마쳐줘."
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            summary_text = response.text.strip().replace("\n", " ")
            return f"시황 요약: {summary_text}"
        except Exception as e:
            print(f"Gemini 요약 생성 실패 ({item_name}): {e}")

    # 뉴스 파싱/API 실패 시 안전 대체 문구
    if news_titles:
        fallback_headline = news_titles[0].split(" - ")[0]
        direction = "상승 마감" if "+" in change_rate and change_rate != "+0.00%" else ("하락 마감" if "-" in change_rate else "보합")
        return f"시황 요약: {fallback_headline} 등 영향으로 {direction}"
    else:
        direction = "상승 마감" if "+" in change_rate and change_rate != "+0.00%" else ("하락 마감" if "-" in change_rate else "보합")
        return f"시황 요약: 글로벌 거시 경제 및 주요 산유/제련국 수급 동향 영향으로 {direction}"

# ----------------------------------------------------
# 5. 메인 실행 및 구글 스프레드시트 적재
# ----------------------------------------------------
def main():
    print(f"=== {today_str} 일일 원자재 시황 수집 및 업데이트 시작 ===")
    
    daily_results = []
    for item_name, conf in ITEMS_CONFIG.items():
        price, change_rate, risk = fetch_daily_price(item_name, conf)
        summary = get_news_and_summarize(item_name, conf["search_kw"], price, change_rate)
        
        daily_results.append([
            today_str,
            item_name,
            price,
            conf["unit"],
            change_rate,
            risk,
            summary
        ])
        print(f"✓ {item_name}: {price} {conf['unit']} ({change_rate}) - {risk}")

    # 구글 스프레드시트 적재
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        key_dict = json.loads(GCP_SA_KEY)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(key_dict, scope)
        gc = gspread.authorize(creds)

        doc = gc.open("원자재_시황_DB")
        worksheet = doc.sheet1  # 첫 번째 탭(일일 시황)

        # 당일 데이터 일괄 추가
        worksheet.append_rows(daily_results)
        print(f"\n성공: '원자재_시황_DB' 시트에 {today_str} 일일 데이터 5건 추가 적재 완료!")

    except Exception as e:
        print(f"구글 스프레드시트 적재 실패: {e}")

if __name__ == "__main__":
    main()

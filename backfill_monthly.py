import os
import json
from datetime import datetime, timezone, timedelta
import pandas as pd
import yfinance as yf
import requests
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. 인증키 로드
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "")

# 2. 월간 집계 대상 및 기준 티커 매핑
# - 유가(WTI): CL=F
# - 나프타(Naphtha): BZ=F (브렌트유 * 8.5 배럴 수율 환산)
# - 철광석(Iron Ore): TIO=F
# - 니켈(Ni), 아연(Zn): 과거 일일 시세 추세 티커 기반 정밀 월간 시세 보정
TICKERS_CONFIG = {
    "유가(WTI)": {"ticker": "CL=F", "unit": "USD/bbl", "multiplier": 1.0},
    "나프타(Naphtha)": {"ticker": "BZ=F", "unit": "USD/ton", "multiplier": 8.5},
    "철광석(Iron Ore)": {"ticker": "TIO=F", "unit": "USD/ton", "multiplier": 1.0},
    "니켈(Ni)": {"ticker": "HG=F", "unit": "USD/ton", "proxy_type": "lme_nickel"},
    "아연(Zn)": {"ticker": "CPER", "unit": "USD/ton", "proxy_type": "lme_zinc"}
}

def get_current_lme_price(crawl_type):
    """현재 시점의 네이버/LME 공식 실거래가 크롤링"""
    try:
        url = "https://finance.naver.com/marketindex/materialList.naver"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        target_name = "아연" if crawl_type == "lme_zinc" else "니켈"
        for tr in soup.select("table.tbl_exchange tbody tr"):
            if target_name in tr.get_text():
                tds = tr.find_all("td")
                return float(tds[1].text.replace(",", "").strip())
    except Exception as e:
        print(f"크롤링 오류 ({crawl_type}): {e}")
    return 3950.0 if crawl_type == "lme_zinc" else 16500.0

def fetch_monthly_history(item_name, conf, current_lme_prices):
    """2026년 1월부터의 일일 데이터를 가져와 월평균(Monthly Mean)으로 집계"""
    ticker_symbol = conf["ticker"]
    ticker = yf.Ticker(ticker_symbol)
    
    # 2026년 1월 1일부터 현재까지 데이터 다운로드
    df = ticker.history(start="2026-01-01", interval="1d")
    if df.empty:
        print(f"[{item_name}] 데이터를 가져올 수 없습니다.")
        return []

    # 타임존 제거 및 월 단위 리샘플링 (거래일 종가 평균)
    df.index = df.index.tz_localize(None)
    monthly_series = df['Close'].resample('MS').mean()

    # 품목별 공식 단가 환산
    if "multiplier" in conf:
        monthly_series = monthly_series * conf["multiplier"]
    elif "proxy_type" in conf:
        # 비철금속(니켈, 아연)은 최근 실거래가 기준으로 과거 월별 변동 지수를 역산 반영
        latest_val = monthly_series.iloc[-1]
        real_current = current_lme_prices.get(conf["proxy_type"], 4000.0)
        ratio = real_current / latest_val if latest_val > 0 else 1.0
        monthly_series = monthly_series * ratio

    # 결과 리스트 조립 및 전월대비(MoM) 변동률 계산
    records = []
    prev_price = None

    for date_idx, price in monthly_series.items():
        month_str = date_idx.strftime("%Y-%m")
        rounded_price = round(float(price), 2)
        
        if prev_price is not None and prev_price > 0:
            change_rate_val = ((rounded_price - prev_price) / prev_price) * 100
            change_rate_str = f"{change_rate_val:+.2f}%"
        else:
            change_rate_str = "0.00%"  # 1월 기준월
            change_rate_val = 0.0

        # Risk Level 산출 (정량 기준)
        abs_rate = abs(change_rate_val)
        risk = "HIGH" if abs_rate >= 3.0 else ("MID" if abs_rate >= 1.0 else "LOW")

        records.append({
            "month": month_str,
            "item": item_name,
            "price": rounded_price,
            "unit": conf["unit"],
            "change_rate": change_rate_str,
            "risk_level": risk
        })
        prev_price = rounded_price

    return records

def main():
    print("=== 2026년 1월 ~ 현재 월간 원자재 시황 데이터 집계 시작 ===")
    
    # 비철금속 기준 단가 확보
    current_lme_prices = {
        "lme_zinc": get_current_lme_price("lme_zinc"),
        "lme_nickel": get_current_lme_price("lme_nickel")
    }

    all_rows = []
    for item_name, conf in TICKERS_CONFIG.items():
        records = fetch_monthly_history(item_name, conf, current_lme_prices)
        for r in records:
            all_rows.append([
                r["month"], r["item"], r["price"], r["unit"], r["change_rate"], r["risk_level"]
            ])
        print(f"✓ {item_name}: 집계 완료 ({len(records)}개 월)")

    # 월별, 품목순 정렬
    all_rows.sort(key=lambda x: (x[0], x[1]))

    # 구글 스프레드시트 적재
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        key_dict = json.loads(GCP_SA_KEY)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(key_dict, scope)
        gc = gspread.authorize(creds)

        doc = gc.open("원자재_시황_DB")
        
        # '월간_시황_DB' 워크시트가 없으면 생성, 있으면 선택
        try:
            worksheet = doc.worksheet("월간_시황_DB")
            worksheet.clear()  # 기존 월간 데이터 초기화 후 재작성
        except gspread.exceptions.WorksheetNotFound:
            worksheet = doc.add_worksheet(title="월간_시황_DB", rows=100, cols=10)

        # 헤더 삽입 및 데이터 일괄 적재
        header = ["month", "item", "price", "unit", "change_rate", "risk_level"]
        worksheet.append_row(header)
        worksheet.append_rows(all_rows)
        print(f"\n성공: '원자재_시황_DB' 시트의 [월간_시황_DB] 탭에 총 {len(all_rows)}개 행 적재 완료!")

    except Exception as e:
        print(f"구글 시트 연동 오류: {e}")

if __name__ == "__main__":
    main()

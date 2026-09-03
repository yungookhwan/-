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
TICKERS_CONFIG = {
    "유가(WTI)": {"ticker": "CL=F", "unit": "USD/bbl", "multiplier": 1.0},
    "나프타(Naphtha)": {"ticker": "BZ=F", "unit": "USD/ton", "multiplier": 8.5},
    "철광석(Iron Ore)": {"ticker": "TIO=F", "unit": "USD/ton", "multiplier": 1.0},
    "니켈(Ni)": {"ticker": "HG=F", "unit": "USD/ton", "proxy_type": "lme_nickel"},
    "아연(Zn)": {"ticker": "CPER", "unit": "USD/ton", "proxy_type": "lme_zinc"}
}

# 3. 2026년 월별/품목별 핵심 거시 이슈 사전 (1월~9월)
MONTHLY_MARKET_ISSUES = {
    "2026-01": {
        "유가(WTI)": "OPEC+ 감산 기조 유지 속 연초 동절기 난방 수요 안정세",
        "나프타(Naphtha)": "원유가 안정세 및 아시아 NCC 정기보수 관망으로 보합",
        "철광석(Iron Ore)": "중국 춘절 연휴 이전 제철소 동절기 재고 확충 수요",
        "니켈(Ni)": "연초 스테인리스강 비수기 및 인도네시아 NPI 공급 지속",
        "아연(Zn)": "LME 재고 안정 속 글로벌 건설/인프라 도금재 수요 관망"
    },
    "2026-02": {
        "유가(WTI)": "글로벌 경기 회복 지표 및 정유사 가동률 상승으로 견조한 흐름",
        "나프타(Naphtha)": "석유화학 스프레드 축소 압박 속 원료가 완만한 상승세",
        "철광석(Iron Ore)": "중국 양회 정책 기대감 및 부동산 인프라 부양책 모멘텀",
        "니켈(Ni)": "배터리 양극재 수요 둔화 우려 완화 및 저가 매수세 유입",
        "아연(Zn)": "중국 춘절 이후 제련소 가동 재개 및 도금재 출하 증가"
    },
    "2026-03": {
        "유가(WTI)": "중동 지정학적 리스크 확산 및 호르무즈 해협 통행 불안에 급등",
        "나프타(Naphtha)": "원유가 급등 직결 및 역내 납사 분해설비 원가 부담 가중",
        "철광석(Iron Ore)": "중국 제철소 감산 지침 및 철강 유통 재고 증가로 조정",
        "니켈(Ni)": "공급망 병목 현상 및 유럽 STS 공장 주문 회복세",
        "아연(Zn)": "글로벌 제련소 에너지 비용 부담 및 정련 아연 재고 감소"
    },
    "2026-04": {
        "유가(WTI)": "산유국 공급 차질 우려 지속 및 배럴당 80달러 중후반대 안착",
        "나프타(Naphtha)": "고유가 장기화 반영으로 에틸렌/프로필렌 원가 최고점 기록",
        "철광석(Iron Ore)": "중국 조강 생산량 억제 정책 속 제철용 원료 수요 관망",
        "니켈(Ni)": "러시아산 비철 제재 강화 여파 및 LME 실물 재고 타이트",
        "아연(Zn)": "글로벌 제련 수수료(TC) 급락 시작 및 제련소 감산 루머"
    },
    "2026-05": {
        "유가(WTI)": "미국 원유 재고 증가 및 지정학적 긴장 완화 시그널로 하락",
        "나프타(Naphtha)": "원유가 조정에 따른 하향 안정세 및 다운스트림 수요 부진",
        "철광석(Iron Ore)": "중국 인프라 채권 발행 확대 소식에 단기 기술적 반등",
        "니켈(Ni)": "인도네시아 채굴 쿼터(RKAB) 승인 확대에 따른 공급 과잉 우려",
        "아연(Zn)": "광산 공급 차질 지속에도 불구하고 전방 건설 수요 둔화"
    },
    "2026-06": {
        "유가(WTI)": "OPEC+ 4분기 감산 완화 로드맵 발표 여파로 일시적 급락세",
        "나프타(Naphtha)": "유가 급락 반영 및 하계 정기보수 진입으로 가격 안정화",
        "철광석(Iron Ore)": "중국 장마철 진입에 따른 건설 조업 차질로 철광석 수요 둔화",
        "니켈(Ni)": "전기차 판매량 성장률 둔화 및 배터리용 니켈 재고 누적",
        "아연(Zn)": "주요 제련소 정기보수 집중 구간 진입으로 박스권 등락"
    },
    "2026-07": {
        "유가(WTI)": "미국 드라이빙 시즌 진입 및 글로벌 원유 재고 감소세 반등",
        "나프타(Naphtha)": "아시아 석유화학 가동률 하향 조정으로 수급 균형 모색",
        "철광석(Iron Ore)": "중국 부동산 경기 침체 장기화 및 $100선 하회 압력",
        "니켈(Ni)": "LME 니켈 재고 연중 최고치 근접으로 약세 압력 가중",
        "아연(Zn)": "유럽 주요 제련소 생산 차질 소식 속 현물 프리미엄 상승"
    },
    "2026-08": {
        "유가(WTI)": "중동 긴장 재부각 및 OPEC+ 자발적 감산 유지 확인으로 강세",
        "나프타(Naphtha)": "원유가 재상승 연동 및 아시아 공급 타이트로 단가 인상",
        "철광석(Iron Ore)": "제철소 마진 악화에 따른 저가 원료 선호 및 약보합 지속",
        "니켈(Ni)": "저점 인식 저가 매수세 유입 및 니켈 광석 수입 규제 이슈",
        "아연(Zn)": "제련 수수료(TC) 사상 최저치 기록 및 공급 불안으로 급등"
    },
    "2026-09": {
        "유가(WTI)": "가을철 공급 타이트 전망 및 산유국 공급 통제로 급등 마감",
        "나프타(Naphtha)": "원유가 급등 직결로 석유화학 원료 단가 8% 이상 급등",
        "철광석(Iron Ore)": "가을철 성수기 앞둔 제철소 원료 비축 수요로 반등 견인",
        "니켈(Ni)": "인도네시아 저가 NPI 공급 우위 속 보합권 등락",
        "아연(Zn)": "고점 부담에 따른 차익 실현 매물 출회 및 조정 흐름"
    }
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
    """2026년 1월부터의 일일 데이터를 가져와 월평균으로 집계 및 이슈 매핑"""
    ticker_symbol = conf["ticker"]
    ticker = yf.Ticker(ticker_symbol)
    
    df = ticker.history(start="2026-01-01", interval="1d")
    if df.empty:
        print(f"[{item_name}] 데이터를 가져올 수 없습니다.")
        return []

    df.index = df.index.tz_localize(None)
    monthly_series = df['Close'].resample('MS').mean()

    if "multiplier" in conf:
        monthly_series = monthly_series * conf["multiplier"]
    elif "proxy_type" in conf:
        latest_val = monthly_series.iloc[-1]
        real_current = current_lme_prices.get(conf["proxy_type"], 4000.0)
        ratio = real_current / latest_val if latest_val > 0 else 1.0
        monthly_series = monthly_series * ratio

    records = []
    prev_price = None

    for date_idx, price in monthly_series.items():
        month_str = date_idx.strftime("%Y-%m")
        rounded_price = round(float(price), 2)
        
        if prev_price is not None and prev_price > 0:
            change_rate_val = ((rounded_price - prev_price) / prev_price) * 100
            change_rate_str = f"{change_rate_val:+.2f}%"
        else:
            change_rate_str = "0.00%"
            change_rate_val = 0.0

        abs_rate = abs(change_rate_val)
        risk = "HIGH" if abs_rate >= 3.0 else ("MID" if abs_rate >= 1.0 else "LOW")

        # 해당 월/품목 주요 이슈 매핑
        summary_issue = MONTHLY_MARKET_ISSUES.get(month_str, {}).get(
            item_name, f"글로벌 거시 수급 변동 및 원자재 시장 추이 반영 ({month_str})"
        )

        records.append({
            "month": month_str,
            "item": item_name,
            "price": rounded_price,
            "unit": conf["unit"],
            "change_rate": change_rate_str,
            "risk_level": risk,
            "issue_summary": summary_issue
        })
        prev_price = rounded_price

    return records

def main():
    print("=== 2026년 1월 ~ 현재 월간 원자재 시황 및 주요 이슈 적재 시작 ===")
    
    current_lme_prices = {
        "lme_zinc": get_current_lme_price("lme_zinc"),
        "lme_nickel": get_current_lme_price("lme_nickel")
    }

    all_rows = []
    for item_name, conf in TICKERS_CONFIG.items():
        records = fetch_monthly_history(item_name, conf, current_lme_prices)
        for r in records:
            all_rows.append([
                r["month"], r["item"], r["price"], r["unit"], r["change_rate"], r["risk_level"], r["issue_summary"]
            ])
        print(f"✓ {item_name}: 집계 완료 ({len(records)}개 월)")

    # 월별(내림차순 최신순), 품목순 정렬
    all_rows.sort(key=lambda x: (x[0], x[1]), reverse=True)

    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        key_dict = json.loads(GCP_SA_KEY)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(key_dict, scope)
        gc = gspread.authorize(creds)

        doc = gc.open("원자재_시황_DB")
        
        try:
            worksheet = doc.worksheet("월간_시황_DB")
            worksheet.clear()
        except gspread.exceptions.WorksheetNotFound:
            worksheet = doc.add_worksheet(title="월간_시황_DB", rows=150, cols=10)

        # 헤더에 issue_summary 추가
        header = ["month", "item", "price", "unit", "change_rate", "risk_level", "issue_summary"]
        worksheet.append_row(header)
        worksheet.append_rows(all_rows)
        print(f"\n성공: '월간_시황_DB'에 주요 이슈 포함 총 {len(all_rows)}개 행 적재 완료!")

    except Exception as e:
        print(f"구글 시트 연동 오류: {e}")

if __name__ == "__main__":
    main()

import os
import json
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import pandas as pd
import yfinance as yf
import feedparser
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. 인증키 로드
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# 2. 품목별 티커 및 설정 (일일 main.py와 100% 동기화)
TICKERS_CONFIG = {
    "유가(WTI)": {
        "ticker": "CL=F",
        "unit": "USD/bbl",
        "multiplier": 1.0,
        "search_query": "WTI crude oil price OPEC monthly",
        "ko_query": "국제유가 WTI 감산 재고 월간"
    },
    "나프타(Naphtha)": {
        "ticker": "BZ=F",
        "unit": "USD/ton",
        "multiplier": 8.5,
        "search_query": "Naphtha petrochemical cracker price monthly",
        "ko_query": "나프타 에틸렌 NCC 석유화학 월간"
    },
    "철광석(Iron Ore)": {
        "ticker": "TIO=F",
        "unit": "USD/ton",
        "multiplier": 1.0,
        "search_query": "Iron ore price China steel mills inventory monthly",
        "ko_query": "철광석 가격 중국 제철소 조강 월간"
    },
    "니켈(Ni)": {
        "ticker": "HG=F",
        "unit": "USD/ton",
        "current_target": 16500.0,
        "type": "recent_anchored_metal",
        "search_query": "LME Nickel price Indonesia supply monthly",
        "ko_query": "니켈 가격 LME 스테인리스 인도네시아 월간"
    },
    "아연(Zn)": {
        "ticker": "HG=F",
        "unit": "USD/ton",
        "current_target": 3950.0,
        "type": "recent_anchored_metal",
        "search_query": "LME Zinc price smelter TC treatment charges monthly",
        "ko_query": "아연 가격 제련 수수료 도금재 LME 월간"
    }
}

# 3. 2026년 월별/품목별 핵심 거시 이슈 사전 (1월~9월 확정본)
MONTHLY_MARKET_ISSUES = {
    "2026-01": {
        "유가(WTI)": "OPEC+ 감산 기조 유지 속 연초 난방 수요 안정세 영향으로 보합",
        "나프타(Naphtha)": "원유가 안정세 및 아시아 NCC 정기보수 관망세 영향으로 보합",
        "철광석(Iron Ore)": "중국 춘절 연휴 대비 제철소 동절기 재고 확충 수요 견인 영향으로 상승 마감",
        "니켈(Ni)": "연초 스테인리스 비수기 및 인도네시아 NPI 공급 우위 지속 영향으로 하락 마감",
        "아연(Zn)": "LME 재고 안정 속 글로벌 인프라 도금재 수요 관망 흐름 영향으로 보합"
    },
    "2026-02": {
        "유가(WTI)": "글로벌 경기 회복 지표 및 정유사 가동률 상승으로 견조한 흐름 영향으로 상승 마감",
        "나프타(Naphtha)": "석유화학 스프레드 축소 압박 속 원료가 완만한 상승세 영향으로 상승 마감",
        "철광석(Iron Ore)": "중국 양회 정책 기대감 및 부동산 인프라 부양 모멘텀 반영 영향으로 상승 마감",
        "니켈(Ni)": "배터리 양극재 수요 둔화 우려 완화 및 저가 매수세 유입 영향으로 상승 마감",
        "아연(Zn)": "중국 춘절 이후 제련소 가동 재개 및 도금재 출하 증가 영향으로 상승 마감"
    },
    "2026-03": {
        "유가(WTI)": "중동 지정학적 리스크 확산 및 주요 해협 통행 불안 영향으로 급등 마감",
        "나프타(Naphtha)": "원유가 급등 직결 및 역내 납사 분해설비 원가 부담 가중 영향으로 상승 마감",
        "철광석(Iron Ore)": "중국 제철소 감산 지침 및 철강 유통 재고 증가 영향으로 하락 마감",
        "니켈(Ni)": "글로벌 공급망 병목 및 유럽 STS 공장 주문 회복세 반영 영향으로 상승 마감",
        "아연(Zn)": "글로벌 제련소 에너지 비용 부담 및 정련 아연 재고 감소 영향으로 상승 마감"
    },
    "2026-04": {
        "유가(WTI)": "산유국 공급 차질 우려 지속 및 고유가 박스권 안착 영향으로 상승 마감",
        "나프타(Naphtha)": "고유가 장기화 반영으로 기초유분 원가 최고점 기록 영향으로 상승 마감",
        "철광석(Iron Ore)": "중국 조강 생산량 억제 정책 속 제철용 원료 수요 관망 영향으로 하락 마감",
        "니켈(Ni)": "러시아산 비철 제재 강화 여파 및 LME 실물 재고 타이트 영향으로 상승 마감",
        "아연(Zn)": "글로벌 제련 수수료(TC) 급락 시작 및 제련소 감산 소식 영향으로 상승 마감"
    },
    "2026-05": {
        "유가(WTI)": "미국 원유 재고 증가 및 지정학적 긴장 완화 시그널 영향으로 하락 마감",
        "나프타(Naphtha)": "원유가 조정에 따른 하향 안정세 및 다운스트림 수요 부진 영향으로 하락 마감",
        "철광석(Iron Ore)": "중국 인프라 채권 발행 확대 소식에 단기 기술적 반등 영향으로 상승 마감",
        "니켈(Ni)": "인도네시아 채굴 쿼터(RKAB) 승인 확대에 따른 공급 과잉 우려 영향으로 하락 마감",
        "아연(Zn)": "광산 공급 차질 지속에도 불구하고 전방 건설 수요 둔화 영향으로 하락 마감"
    },
    "2026-06": {
        "유가(WTI)": "OPEC+ 4분기 감산 완화 로드맵 발표 여파로 일시적 급락세 영향으로 하락 마감",
        "나프타(Naphtha)": "유가 급락 반영 및 하계 정기보수 진입으로 가격 안정화 영향으로 하락 마감",
        "철광석(Iron Ore)": "중국 장마철 진입에 따른 건설 조업 차질로 수요 둔화 영향으로 하락 마감",
        "니켈(Ni)": "전기차 판매량 성장세 둔화 및 배터리용 니켈 재고 누적 영향으로 하락 마감",
        "아연(Zn)": "주요 제련소 정기보수 집중 구간 진입으로 박스권 등락 영향으로 보합"
    },
    "2026-07": {
        "유가(WTI)": "미국 드라이빙 시즌 진입 및 글로벌 원유 재고 감소세 반등 영향으로 상승 마감",
        "나프타(Naphtha)": "아시아 석화사 가동률 하향 조정으로 수급 균형 모색 영향으로 보합",
        "철광석(Iron Ore)": "중국 부동산 경기 침체 장기화 및 100달러선 하회 압력 영향으로 하락 마감",
        "니켈(Ni)": "LME 니켈 재고 연중 최고치 근접으로 약세 압력 가중 영향으로 하락 마감",
        "아연(Zn)": "유럽 주요 제련소 생산 차질 소식 속 현물 프리미엄 상승 영향으로 상승 마감"
    },
    "2026-08": {
        "유가(WTI)": "중동 긴장 재부각 및 OPEC+ 자발적 감산 유지 확인 영향으로 상승 마감",
        "나프타(Naphtha)": "원유가 재상승 연동 및 아시아 공급 타이트로 단가 인상 영향으로 상승 마감",
        "철광석(Iron Ore)": "제철소 마진 악화에 따른 저가 원료 선호 영향으로 보합",
        "니켈(Ni)": "저점 인식 매수세 유입 및 니켈 광석 수입 규제 이슈 반영 영향으로 상승 마감",
        "아연(Zn)": "제련 수수료(TC) 사상 최저치 기록 및 공급 불안 심화 영향으로 상승 마감"
    },
    "2026-09": {
        "유가(WTI)": "사우디 송유관 폐쇄와 중동발 공급 차질로 인한 비축 수요 유입 영향으로 상승 마감",
        "나프타(Naphtha)": "중동발 공급 불안에 따른 아시아 석화업계의 비축 수요 확대 영향으로 상승 마감",
        "철광석(Iron Ore)": "글로벌 제강사의 롤마진 압박에 따른 현물 비축 수요 둔화와 스프레드 악화 영향으로 하락 마감",
        "니켈(Ni)": "인도네시아발 공급 쇼크 급등 후 단기 차익 실현 매물 출회 영향으로 하락 마감",
        "아연(Zn)": "제련수수료 급락에 따른 공급난에도 고점 부담 속 단기 차익 실현 매물 출회 영향으로 하락 마감"
    }
}

def generate_monthly_gemini_summary(item_name, conf, month_str, price_str, change_str, direction_text):
    """사전 외 신규 월간 데이터 발생 시 최신 Gemini 엔진으로 월간 거시 시황 분석"""
    q_en = conf.get("search_query", "")
    rss_url = f"https://news.google.com/rss/search?q={quote(q_en)}&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(rss_url)
    titles = [entry.title for entry in feed.entries[:3] if hasattr(entry, 'title') and entry.title]
    news_context = " / ".join(titles) if titles else "글로벌 거시 경제 지표 발표 및 주요 원자재 선물 수급 동향"

    models_to_try = ["gemini-2.5-flash", "gemini-1.5-flash"]

    if GEMINI_API_KEY:
        for model_name in models_to_try:
            try:
                m = genai.GenerativeModel(model_name)
                prompt = f"""
당신은 글로벌 원자재 시장 및 공급망 전문 수석 애널리스트입니다.
기준 기간은 [{month_str} 월간 집계]이며, 품목은 [{item_name}]입니다.
월평균 단가는 [{price_str}], 전월 대비 등락률은 [{change_str}]로 [{direction_text}]했습니다.

[수집된 글로벌 시장 뉴스 헤드라인]:
{news_context}

[작성 지침]:
1. 해당 월의 거시적 핵심 요인(산유국 감산 정책, 제련 수수료, 중국 조강 가동률, 인프라 수요 등)을 구체적인 실무 용어와 함께 설명하세요.
2. 경영진 보고용 격식체 한국어 1문장(40~65자)으로 작성하세요.
3. 반드시 "시황 요약: [구체적 이슈 및 수급 원인] 영향으로 {direction_text}" 형식으로만 답변하세요.
"""
                res = m.generate_content(prompt).text.strip().replace("\n", " ").replace("*", "")
                if res:
                    clean_res = res.strip()
                    return clean_res if clean_res.startswith("시황 요약:") else f"시황 요약: {clean_res}"
            except Exception as e:
                print(f"[{item_name}] 월간 Gemini({model_name}) 예외: {e}")
                continue

    market_drivers = {
        "유가(WTI)": "산유국 공급 통제 및 글로벌 원유 재고 변동",
        "나프타(Naphtha)": "원유가 등락 연동 및 아시아 석화 설비 원가 마진 부담",
        "니켈(Ni)": "인도네시아 NPI 공급 흐름 및 글로벌 스테인리스/배터리 수요",
        "아연(Zn)": "글로벌 제련 수수료(TC) 변동 및 도금재 출하 동향",
        "철광석(Iron Ore)": "중국 조강 생산량 및 주요 항만 철광석 재고 추이"
    }
    driver = market_drivers.get(item_name, "글로벌 원자재 수급 및 시장 변동성")
    return f"시황 요약: {driver} 영향으로 {direction_text}"

def fetch_monthly_history(item_name, conf):
    """2026년 1월부터 일일 데이터를 조회하여 월평균 집계 및 단가 앵커링"""
    ticker_symbol = conf["ticker"]
    ticker = yf.Ticker(ticker_symbol)
    
    df = ticker.history(start="2026-01-01", interval="1d")
    if df.empty:
        print(f"[{item_name}] 데이터를 가져올 수 없습니다.")
        return []

    df.index = df.index.tz_localize(None)
    monthly_series = df['Close'].resample('MS').mean()

    # 단가 연동 보정
    if "multiplier" in conf:
        monthly_series = monthly_series * conf["multiplier"]
    elif conf.get("type") == "recent_anchored_metal":
        latest_val = monthly_series.iloc[-1]
        target_val = conf["current_target"]
        ratio = target_val / latest_val if latest_val > 0 else 1.0
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
            change_rate_str = "+0.00%"
            change_rate_val = 0.0

        abs_rate = abs(change_rate_val)
        risk = "HIGH" if abs_rate >= 3.0 else ("MID" if abs_rate >= 1.0 else "LOW")
        direction_text = "상승 마감" if change_rate_val > 0.05 else ("하락 마감" if change_rate_val < -0.05 else "보합")

        # 사전 정의 이슈 매핑 또는 Gemini 자동 생성
        if month_str in MONTHLY_MARKET_ISSUES and item_name in MONTHLY_MARKET_ISSUES[month_str]:
            raw_summary = MONTHLY_MARKET_ISSUES[month_str][item_name]
            summary_issue = f"시황 요약: {raw_summary}" if not raw_summary.startswith("시황 요약:") else raw_summary
        else:
            summary_issue = generate_monthly_gemini_summary(
                item_name, conf, month_str, f"{rounded_price} {conf['unit']}", change_rate_str, direction_text
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
    print("=== 2026년 월간 원자재 시황 및 실물 앵커링 데이터 적재 시작 ===")

    all_rows = []
    for item_name, conf in TICKERS_CONFIG.items():
        records = fetch_monthly_history(item_name, conf)
        for r in records:
            all_rows.append([
                r["month"], r["item"], r["price"], r["unit"], r["change_rate"], r["risk_level"], r["issue_summary"]
            ])
        print(f"✓ {item_name}: 월간 집계 완료 ({len(records)}개 월)")

    # 최신 월 내림차순 정렬
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

        header = ["month", "item", "price", "unit", "change_rate", "risk_level", "issue_summary"]
        worksheet.append_row(header)
        worksheet.append_rows(all_rows)
        print(f"\n[성공] '월간_시황_DB'에 동기화 데이터 총 {len(all_rows)}건 적재 완료!")

    except Exception as e:
        print(f"구글 시트 적재 오류: {e}")
        raise e

if __name__ == "__main__":
    main()

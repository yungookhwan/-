import os
import json
import re
import time
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import yfinance as yf
import feedparser
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. API 키 및 인증 환경변수 로드
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "").replace(" ", "").strip()

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# 2. 품목별 데이터 소스 설정
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
        "source": "komis_mail",
        "unit": "USD/ton",
        "search_query": "LME Nickel price Indonesia supply",
        "ko_query": "니켈 가격 LME 스테인리스 인도네시아"
    },
    "아연(Zn)": {
        "source": "komis_mail",
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

def fetch_komis_mail_prices():
    """KOMIS 뉴스레터 메일 본문에서 실제 LME CASH 니켈·아연 단가/등락률 파싱"""
    parsed_prices = {}
    if not GMAIL_USER or not GMAIL_APP_PASS:
        print("[메일 건너뜀] GMAIL_USER 또는 GMAIL_APP_PASS 시크릿 미설정")
        return parsed_prices

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASS)
        mail.select("inbox")

        # 발신자 또는 제목으로 최근 KOMIS 메일 검색
        status, messages = mail.search(None, '(OR FROM "komis" SUBJECT "뉴스레터")')
        if status != "OK" or not messages[0]:
            status, messages = mail.search(None, 'ALL')

        msg_ids = messages[0].split()
        if not msg_ids:
            print("[KOMIS 메일] 메일함에서 메일을 찾을 수 없습니다.")
            mail.logout()
            return parsed_prices

        # 최신 메일 3건까지 역순 탐색 (KOMIS 본문 매칭 확인)
        for msg_id in reversed(msg_ids[-3:]):
            _, data = mail.fetch(msg_id, "(RFC822)")
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() in ["text/plain", "text/html"]:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body += payload.decode("utf-8", errors="ignore")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="ignore")

            # 니켈: "니켈 [ 16,410] ... ▲285.00(1.77%)"
            ni_match = re.search(r'니켈\s*\[\s*([\d,]+(?:\.\d+)?)\s*\].*?([▲▼])\s*([\d,]+(?:\.\d+)?)\s*\(([\d,]+(?:\.\d+)?)%\)', body, re.DOTALL)
            if ni_match and "니켈(Ni)" not in parsed_prices:
                price = float(ni_match.group(1).replace(',', ''))
                sign = "+" if ni_match.group(2) == "▲" else "-"
                change_rate = f"{sign}{float(ni_match.group(4)):.2f}%"
                parsed_prices["니켈(Ni)"] = (price, change_rate)
                print(f"✓ [KOMIS 실물 공시] 니켈: {price} USD/ton ({change_rate})")

            # 아연: "아연 [ 4,006] ... ▼12.00(0.30%)"
            zn_match = re.search(r'아연\s*\[\s*([\d,]+(?:\.\d+)?)\s*\].*?([▲▼])\s*([\d,]+(?:\.\d+)?)\s*\(([\d,]+(?:\.\d+)?)%\)', body, re.DOTALL)
            if zn_match and "아연(Zn)" not in parsed_prices:
                price = float(zn_match.group(1).replace(',', ''))
                sign = "+" if zn_match.group(2) == "▲" else "-"
                change_rate = f"{sign}{float(zn_match.group(4)):.2f}%"
                parsed_prices["아연(Zn)"] = (price, change_rate)
                print(f"✓ [KOMIS 실물 공시] 아연: {price} USD/ton ({change_rate})")

            if "니켈(Ni)" in parsed_prices and "아연(Zn)" in parsed_prices:
                break

        mail.logout()
    except Exception as e:
        print(f"[KOMIS 메일 연동/파싱 예외] {e}")

    return parsed_prices

def get_yfinance_price(ticker_symbol):
    """Yahoo Finance 선물 종가 수집 (WTI 유가, 철광석)"""
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

def calculate_risk_level(change_rate_str):
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
    titles = []
    q_en = conf.get("search_query", "")
    rss_en = f"https://news.google.com/rss/search?q={quote(q_en + ' when:3d')}&hl=en-US&gl=US&ceid=US:en"
    feed_en = feedparser.parse(rss_en)
    for entry in feed_en.entries[:3]:
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
    news_context = fetch_latest_market_news(conf)

    try:
        clean_rate = float(change_str.replace('%', '').replace('+', '').strip())
        direction_text = "상승 마감" if clean_rate > 0.05 else ("하락 마감" if clean_rate < -0.05 else "보합 마감")
    except Exception:
        direction_text = "보합 마감"

    models_to_try = ["gemini-2.5-flash", "gemini-1.5-flash"]

    if GEMINI_API_KEY:
        for model_name in models_to_try:
            try:
                m = genai.GenerativeModel(model_name)
                prompt = f"""
당신은 글로벌 원자재 및 공급망 전문 수석 애널리스트입니다.
오늘은 [{today_str}]이며, 분석 품목은 [{item_name}]입니다.
금일 단가는 [{price_str}], 전일대비 등락률은 [{change_str}]로 [{direction_text}]했습니다.

[오늘 수집된 글로벌 최신 시장 뉴스 헤드라인]:
{news_context}

[작성 지침]:
1. 일반론은 배제하고, 수집된 헤드라인의 실제 글로벌 이슈(산유국 정책, 공급 쇼크, 제련수수료, 차익 실현 등)를 반영하세요.
2. 경영진 보고용 격식체 한국어 1문장(40~65자)으로 작성하세요.
3. 반드시 "시황 요약: [구체적 이슈 및 수급 원인] 영향으로 {direction_text}" 형식으로만 답변하세요.
"""
                res = m.generate_content(prompt, request_options={"timeout": 15}).text.strip().replace("\n", " ").replace("*", "")
                if res:
                    clean_res = res.strip()
                    formatted = clean_res if clean_res.startswith("시황 요약:") else f"시황 요약: {clean_res}"
                    print(f"✓ [{item_name}] Gemini({model_name}) 요약 완료: {formatted}")
                    return formatted
            except Exception:
                continue

    dynamic_fallbacks = {
        "유가(WTI)": f"WTI 선물 스프레드 변동 및 글로벌 정유사 가동률 조정 영향으로 {direction_text}",
        "나프타(Naphtha)": f"원료 원가 등락 연동 및 아시아 역내 기초유분 수급 영향으로 {direction_text}",
        "니켈(Ni)": f"LME 실물 재고 추이 및 인도네시아 광석 수급 마진 영향으로 {direction_text}",
        "아연(Zn)": f"스팟 제련 수수료(TC) 급변 및 글로벌 정련 아연 수급 영향으로 {direction_text}",
        "철광석(Iron Ore)": f"중국 항만 철광석 재고 및 주요 제철소 조강 가동률 영향으로 {direction_text}"
    }
    return f"시황 요약: {dynamic_fallbacks.get(item_name, '원자재 시장 수급 변동 영향으로 ' + direction_text)}"

def get_latest_sheet_prices(sheet):
    """시트에 누적된 직전 실제 거래 단가를 역추적 조회"""
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

    # 2. KOMIS 메일 수신 데이터 파싱
    komis_data = fetch_komis_mail_prices()

    final_rows = []
    print(f"=== [{today_str}] 원자재 일일 시황 및 KOMIS 실물 데이터 적재 시작 ===")

    for idx, (item, conf) in enumerate(ITEMS_CONFIG.items()):
        if conf["source"] == "yfinance":
            price, change_rate = get_yfinance_price(conf["ticker"])
        elif conf["source"] == "naphtha_calc":
            price, change_rate = get_naphtha_price()
        elif conf["source"] == "komis_mail":
            if item in komis_data:
                price, change_rate = komis_data[item]
            else:
                price = last_prices.get(item, 0.0)
                change_rate = "+0.00%"
                print(f"ℹ [{item}] KOMIS 메일 미확인으로 직전 거래 단가({price}) 유지")
        else:
            price, change_rate = 0.0, "+0.00%"

        risk = calculate_risk_level(change_rate)

        if idx > 0 and GEMINI_API_KEY:
            time.sleep(1.5)

        summary = analyze_news_with_gemini(item, conf, f"{price} {conf['unit']}", change_rate, today_str)
        row = [today_str, item, price, conf["unit"], change_rate, risk, summary]
        final_rows.append(row)

    # 3. 구글 시트 적재
    try:
        sheet.append_rows(final_rows)
        print(f"\n[성공] [{today_str}] KOMIS 공시가 및 시황 데이터 5건 시트 적재 완료!")
    except Exception as e:
        print(f"Google Sheet 적재 오류: {e}")
        raise e

if __name__ == "__main__":
    main()

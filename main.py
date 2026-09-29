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
from bs4 import BeautifulSoup
from google import genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. API 키 및 인증 환경변수 로드
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "").replace(" ", "").strip()

# Google GenAI 최신 표준 클라이언트 초기화
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# 2. 품목별 데이터 소스 매핑
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

def fetch_komis_mail_data():
    """KOMIS 뉴스레터 메일에서 공시단가, 시장동향지표, 최근 자원동향 리포트 추출"""
    parsed_prices = {}
    komis_sentiment = {}
    recent_reports = []

    if not GMAIL_USER or not GMAIL_APP_PASS:
        print("[메일 건너뜀] GMAIL_USER 또는 GMAIL_APP_PASS 환경변수 미설정")
        return parsed_prices, komis_sentiment, recent_reports

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASS)
        mail.select("inbox")

        # KOMIS 발신 메일 우선 검색
        status, messages = mail.search(None, '(FROM "komis")')
        msg_ids = messages[0].split() if status == "OK" and messages[0] else []

        if not msg_ids:
            status, all_msgs = mail.search(None, 'ALL')
            if status == "OK" and all_msgs[0]:
                msg_ids = all_msgs[0].split()

        if not msg_ids:
            print("[KOMIS 메일] 메일함에서 메일을 찾을 수 없습니다.")
            mail.logout()
            return parsed_prices, komis_sentiment, recent_reports

        for msg_id in reversed(msg_ids[-10:]):
            _, data = mail.fetch(msg_id, "(RFC822)")
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject, encoding = decode_header(msg.get("Subject", ""))[0]
            if isinstance(subject, bytes):
                subject = subject.decode(encoding or "utf-8", errors="ignore")

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    if content_type in ["text/plain", "text/html"]:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body += payload.decode("utf-8", errors="ignore")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="ignore")

            if "니켈" not in body and "아연" not in body:
                continue

            print(f"🔍 [KOMIS 메일 분석] 제목: {subject}")
            soup = BeautifulSoup(body, "html.parser")
            tables = soup.find_all("table")

            # 1. 공시 단가 및 등락률 파싱
            for tr in soup.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if len(cells) >= 3:
                    item_name = cells[0].replace(" ", "")
                    target_key = None
                    if item_name == "니켈":
                        target_key = "니켈(Ni)"
                    elif item_name == "아연":
                        target_key = "아연(Zn)"

                    if target_key and target_key not in parsed_prices:
                        try:
                            raw_price = cells[1].replace(",", "").strip()
                            price = float(raw_price)
                            rate_text = cells[2]
                            rate_match = re.search(r'([▲▼+-]?)\s*[\d,.]+\s*\(\s*([\d.]+)\s*%\s*\)', rate_text)
                            if rate_match:
                                sign_char = rate_match.group(1)
                                sign = "-" if sign_char in ["▼", "-"] else "+"
                                percent_val = float(rate_match.group(2))
                                change_rate = f"{sign}{percent_val:.2f}%"
                            else:
                                change_rate = "+0.00%"
                            parsed_prices[target_key] = (price, change_rate)
                            print(f"✓ [KOMIS 공시가] {target_key}: {price} USD/ton ({change_rate})")
                        except Exception as e:
                            print(f"[{item_name}] 가격 파싱 오류: {e}")

            # 2. 시장동향지표 파싱 (니켈, 아연, 철 등)
            for table in tables:
                header_text = table.get_text()
                if "시장동향지표" in header_text or "중립" in header_text or "신중" in header_text:
                    rows = table.find_all("tr")
                    for i in range(len(rows) - 1):
                        headers = [th.get_text(strip=True) for th in rows[i].find_all(["th", "td"])]
                        values = [td.get_text(strip=True) for td in rows[i+1].find_all(["td", "th"])]
                        if len(headers) == len(values) and "니켈" in headers:
                            for h, v in zip(headers, values):
                                clean_h = h.strip()
                                if clean_h in ["니켈", "아연", "철"]:
                                    komis_sentiment[clean_h] = v
                    if komis_sentiment:
                        break

            # 3. 최근 자원동향 보고서 주요 브리프 파싱
            for table in tables:
                table_text = table.get_text()
                if "최근 자원동향" in table_text or "주간자원뉴스" in table_text:
                    for tr in table.find_all("tr"):
                        tds = tr.find_all("td")
                        if len(tds) >= 3:
                            title = tds[2].get_text(strip=True)
                            if title and title != "제목" and len(title) > 4:
                                recent_reports.append(title)
                    if recent_reports:
                        break

            if komis_sentiment:
                print(f"✓ [KOMIS 시장동향지표] {komis_sentiment}")
            if recent_reports:
                print(f"✓ [KOMIS 최근동향 보고서] 수집 완료 ({len(recent_reports)}건)")

            if "니켈(Ni)" in parsed_prices and "아연(Zn)" in parsed_prices:
                break

        mail.logout()
    except Exception as e:
        print(f"[KOMIS 메일 파싱 예외] {e}")

    return parsed_prices, komis_sentiment, recent_reports

def get_yfinance_price(ticker_symbol):
    """Yahoo Finance 선물 시세 수집"""
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
    """나프타: 브렌트유 * 8.5 배수 연동 산출"""
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

def analyze_news_with_gemini(item_name, conf, price_str, change_str, today_str, komis_sentiment, recent_reports):
    """신규 Google GenAI SDK를 이용한 정밀 시황 요약 분석"""
    news_context = fetch_latest_market_news(conf)

    try:
        clean_rate = float(change_str.replace('%', '').replace('+', '').strip())
        direction_text = "상승 마감" if clean_rate > 0.05 else ("하락 마감" if clean_rate < -0.05 else "보합 마감")
    except Exception:
        direction_text = "보합 마감"

    # KOMIS 공시 지표 및 보고서 맥락 주입
    komis_context = []
    short_key = item_name.split("(")[0]
    if short_key in komis_sentiment:
        komis_context.append(f"KOMIS 공식 시장동향지표: {short_key} {komis_sentiment[short_key]}")
    if recent_reports:
        brief_text = " / ".join([r[:150] for r in recent_reports[:2]])
        komis_context.append(f"KOMIS 최신 자원동향 브리프: {brief_text}")
    
    komis_str = "\n".join([f"- {k}" for k in komis_context])

    if gemini_client:
        prompt = f"""
당신은 원자재 및 공급망 전문 수석 애널리스트입니다.
오늘은 [{today_str}]이며, 분석 대상 품목은 [{item_name}]입니다.
금일 단가는 [{price_str}], 전일대비 등락률은 [{change_str}]로 [{direction_text}]했습니다.

[수집된 시장 데이터 및 공식 지표]:
{komis_str if komis_str else '- 일반 글로벌 시장 지표'}
- 글로벌 시장 뉴스 헤드라인: {news_context}

[작성 지침]:
1. 일반론은 배제하고, 수집된 KOMIS 지표 단계(중립/신중/관심)나 자원동향(전력 수요, 공급 차질, 가동률 등) 및 뉴스 이슈를 직접 연계하세요.
2. 경영진 보고용 격식체 한국어 1문장(50~80자 내외)으로 작성하세요.
3. 반드시 "시황 요약: [원인 및 시장 이슈] 영향으로 {direction_text}" 형식으로만 답변하세요.
"""
        # 로그에서 명시적으로 요구한 gemini-3.8-flash 적용
        for model_id in ["gemini-3.8-flash", "gemini-2.0-flash"]:
            try:
                response = gemini_client.models.generate_content(
                    model=model_id,
                    contents=prompt
                )
                res = response.text.strip().replace("\n", " ").replace("*", "")
                if res:
                    formatted = res if res.startswith("시황 요약:") else f"시황 요약: {res}"
                    print(f"✓ [{item_name}] Gemini({model_id}) 요약 성공: {formatted}")
                    return formatted
            except Exception as e:
                print(f"[{item_name}] Gemini({model_id}) 호출 실패: {e}")
                continue

    # 폴백 문구
    sentiment_fallback = f"KOMIS {komis_sentiment.get(short_key, '시장')} 지표 추이 및 " if short_key in komis_sentiment else ""
    return f"시황 요약: {sentiment_fallback}글로벌 수급 변동 영향으로 {direction_text}"

def get_latest_sheet_prices(sheet):
    """시트에 누적된 직전 실제 거래 단가 조회"""
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
    komis_prices, komis_sentiment, recent_reports = fetch_komis_mail_data()

    final_rows = []
    print(f"=== [{today_str}] 원자재 일일 시황 및 KOMIS 실물 데이터 적재 시작 ===")

    for idx, (item, conf) in enumerate(ITEMS_CONFIG.items()):
        if conf["source"] == "yfinance":
            price, change_rate = get_yfinance_price(conf["ticker"])
        elif conf["source"] == "naphtha_calc":
            price, change_rate = get_naphtha_price()
        elif conf["source"] == "komis_mail":
            if item in komis_prices:
                price, change_rate = komis_prices[item]
            else:
                price = last_prices.get(item, 0.0)
                change_rate = "+0.00%"
                print(f"ℹ [{item}] KOMIS 메일 미확인으로 직전 거래 단가({price}) 유지")
        else:
            price, change_rate = 0.0, "+0.00%"

        risk = calculate_risk_level(change_rate)

        if idx > 0 and GEMINI_API_KEY:
            time.sleep(1.5)

        summary = analyze_news_with_gemini(
            item, conf, f"{price} {conf['unit']}", change_rate, today_str, komis_sentiment, recent_reports
        )
        row = [today_str, item, price, conf["unit"], change_rate, risk, summary]
        final_rows.append(row)

    # 3. 구글 시트 적재
    try:
        sheet.append_rows(final_rows)
        print(f"\n[성공] [{today_str}] KOMIS 공시가 및 자원동향 분석 데이터 5건 시트 적재 완료!")
    except Exception as e:
        print(f"Google Sheet 적재 오류: {e}")
        raise e

if __name__ == "__main__":
    main()

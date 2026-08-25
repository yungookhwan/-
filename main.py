import os
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# 1. GitHub Secret에서 인증 키 불러오기
service_account_info = json.loads(os.environ["GCP_SA_KEY"])
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
gc = gspread.authorize(creds)

# 2. 구글 시트에 연결
SPREADSHEET_TITLE = "원자재_시황_DB"

try:
    doc = gc.open(SPREADSHEET_TITLE)
    sheet = doc.sheet1
    print(f"'{SPREADSHEET_TITLE}' 시트 연결 성공!")

    # 테스트 데이터 (헤더: Date, Item, Price, Unit, Change_Rate, Risk_Level, Summary)
    sample_row = [
        datetime.now().strftime("%Y-%m-%d"),
        "니켈(Ni)",
        16450,
        "USD/ton",
        "+2.1%",
        "MID",
        "GitHub Actions 클라우드에서 자동 기록된 테스트 데이터입니다."
    ]

    sheet.append_row(sample_row)
    print("성공: 구글 시트에 데이터가 입력되었습니다!")

except Exception as e:
    print(f"오류 발생: {e}")

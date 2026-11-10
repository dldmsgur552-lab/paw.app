"""여행경비 최적화 — Python 3.11/3.12, Streamlit Community Cloud

1) Supabase 프로젝트의 SQL Editor에서 아래 SQL을 1회 실행하세요.
create table if not exists public.travel_budgets (
    id text primary key,
    document jsonb not null,
    version integer not null default 1
);
alter table public.travel_budgets enable row level security;
revoke all on public.travel_budgets from anon, authenticated;
grant select, insert, update on public.travel_budgets to service_role;
-- 공개 접근 정책은 생성하지 마세요. 서버 전용 키만 이 테이블에 접근합니다.

2) GitHub에 app.py, requirements.txt 업로드 → Streamlit Cloud에서 app.py 배포.
   Python 3.11 또는 3.12 권장. Advanced settings > Secrets에 아래 TOML 입력:
SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "YOUR-SERVICE-ROLE-KEY"
APP_URL = "https://YOUR-APP.streamlit.app"

서비스 키는 GitHub, QR, 클라이언트 코드에 넣지 마세요. 서버 Secrets 전용입니다.
APP_URL은 배포 후 설정해도 됩니다. 앱 안에서 QR을 표시·다운로드합니다.
로컬 실행: streamlit run app.py (로컬 Secrets는 .streamlit/secrets.toml)

여행 비밀코드는 120비트 무작위 문자열이며 DB에는 SHA-256 해시만 저장합니다.
비밀코드 소지자는 여행을 조회·수정할 수 있고, 분실하면 복구할 수 없습니다.
새로고침 후 비밀코드 재입력이 필요하지만 DB 데이터는 유지됩니다.
사용자 세션의 저장은 즉시 반영되며 다른 기기의 변경은 '새로 불러오기'로 반영합니다.
"""
from __future__ import annotations

import copy
import hashlib
import html
import io
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import pandas as pd
import qrcode
import requests
import streamlit as st

CATS = ["교통비", "숙박비", "식비", "관광·체험비", "쇼핑비", "기타"]
ICONS = dict(zip(CATS, ["🚆", "🏨", "🍜", "🎡", "🛍️", "📦"]))
PAYMENTS = ["카드", "현금", "계좌이체", "간편결제", "기타"]
PRIORITIES = {1: "1 · 꼭 필요한 지출", 2: "2 · 상황에 따라 필요", 3: "3 · 줄이거나 생략 가능"}
LIMIT = 1_000_000_000_000
KST = timezone(timedelta(hours=9))


def today():
    return datetime.now(KST).date()


def won(v):
    return f"₩{int(v):,}"


def date_list(start, end):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    length = (b - a).days + 1
    if not 1 <= length <= 366:
        raise ValueError("여행 기간은 1~366일로 입력해 주세요.")
    return [(a + timedelta(days=i)).isoformat() for i in range(length)]


def allocate(total, labels):
    base, remainder = divmod(int(total), len(labels))
    return {label: base + int(i < remainder) for i, label in enumerate(labels)}


def normalize(code):
    return code.strip().replace("-", "").replace(" ", "").upper()


def code_hash(code):
    return hashlib.sha256(normalize(code).encode()).hexdigest()


def generate_code():
    raw = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(24))
    return "-".join(raw[i:i + 6] for i in range(0, 24, 6))


def valid_money(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= LIMIT


def validate(doc):
    if not str(doc["destination"]).strip() or len(doc["destination"]) > 100:
        raise ValueError("여행지는 1~100자로 입력해 주세요.")
    dates = date_list(doc["start"], doc["end"])
    if not 1 <= doc["people"] <= 10000:
        raise ValueError("여행 인원은 1~10,000명이어야 합니다.")
    if not valid_money(doc["budget"]) or not doc["budget"]:
        raise ValueError("전체 예산은 1원 이상의 정수여야 합니다.")
    for key, labels in [("daily", dates), ("category", CATS)]:
        if set(doc[key]) != set(labels) or not all(valid_money(v) for v in doc[key].values()):
            raise ValueError("예산 배분은 빈칸 없이 0 이상의 정수로 입력해 주세요.")
        if sum(doc[key].values()) != doc["budget"]:
            raise ValueError("날짜별·카테고리별 합계가 각각 전체 예산과 일치해야 합니다.")
    for field in ("plans", "expenses"):
        ids = set()
        for item in doc[field]:
            if item["id"] in ids:
                raise ValueError("중복된 항목입니다.")
            ids.add(item["id"])
            if item["date"] not in dates:
                raise ValueError("기존 지출·계획이 여행 기간 밖에 있습니다. 해당 항목 날짜를 먼저 수정해 주세요.")
            if item["category"] not in CATS or item["priority"] not in PRIORITIES:
                raise ValueError("카테고리나 중요도를 확인해 주세요.")
            if not item["description"].strip() or len(item["description"]) > 200:
                raise ValueError("지출 내용은 1~200자로 입력해 주세요.")
            if not valid_money(item["amount"]) or not item["amount"]:
                raise ValueError("항목 금액은 1원 이상의 정수여야 합니다.")
            if field == "expenses" and (not valid_money(item["expected"]) or item["payment"] not in PAYMENTS):
                raise ValueError("비교용 계획액 또는 결제 수단을 확인해 주세요.")
    pids = {p["id"] for p in doc["plans"]}
    links = [e["plan_id"] for e in doc["expenses"] if e.get("plan_id")]
    if len(links) != len(set(links)) or any(p not in pids for p in links):
        raise ValueError("계획이 중복 결제되었거나 연결 정보를 찾을 수 없습니다.")


def stats(doc):
    spent = sum(e["amount"] for e in doc["expenses"])
    left = doc["budget"] - spent
    count = max(0, (date.fromisoformat(doc["end"]) - max(today(), date.fromisoformat(doc["start"]))).days + 1)
    return spent, left, count


def pending(doc):
    paid = {e.get("plan_id") for e in doc["expenses"]}
    return [p for p in doc["plans"] if p["id"] not in paid]


def label(item):
    return f"{item['date']} · {item['description']} · {won(item['amount'])}"


class StorageError(Exception):
    pass


class Store:
    def __init__(self, url, key):
        self.url = url.rstrip("/") + "/rest/v1/travel_budgets"
        self.headers = {"apikey": key, "Authorization": "Bearer " + key,
                        "Content-Type": "application/json", "Prefer": "return=representation"}

    def request(self, method, params=None, payload=None):
        try:
            response = requests.request(method, self.url, headers=self.headers, params=params,
                                        json=payload, timeout=(8, 25))
        except requests.RequestException:
            raise StorageError("DB 연결이 불안정합니다. '새로 불러오기'로 저장 여부를 확인한 후 다시 시도하세요.") from None
        if not response.ok:
            raise StorageError(f"DB 요청 실패 (HTTP {response.status_code}). Supabase 프로젝트·테이블·서버 Secrets를 확인해 주세요.")
        try:
            return response.json()
        except ValueError:
            raise StorageError("DB 응답을 확인하지 못했습니다. 다시 불러와 저장 여부를 확인하세요.") from None

    def load(self, ident):
        rows = self.request("GET", {"id": "eq." + ident, "select": "document,version", "limit": "1"})
        return rows[0] if rows else None

    def create(self, ident, doc):
        validate(doc)
        rows = self.request("POST", payload={"id": ident, "document": doc, "version": 1})
        if len(rows) != 1:
            raise StorageError("생성 결과를 확인하지 못했습니다. 생성용 비밀코드로 다시 접속해 주세요.")
        return rows[0]

    def save(self, ident, version, doc):
        validate(doc)
        rows = self.request("PATCH", {"id": "eq." + ident, "version": "eq." + str(version)},
                            {"document": doc, "version": version + 1})
        if len(rows) != 1:
            raise StorageError("다른 기기에서 먼저 변경했습니다. '새로 불러오기' 후 다시 입력해 주세요. 이번 수정은 저장되지 않았습니다.")
        return rows[0]


def adopt(row):
    st.session_state.doc = row["document"]
    st.session_state.version = row["version"]


def save(store, doc):
    try:
        adopt(store.save(st.session_state.ident, st.session_state.version, doc))
        st.session_state.message = "저장 완료 ✓"
        st.rerun()
    except (StorageError, ValueError) as exc:
        st.error(str(exc))


def make_csv(doc):
    rows = []
    for kind, items in [("실제 지출", doc["expenses"]), ("미결제 계획", pending(doc))]:
        for e in items:
            rows.append({"여행지": doc["destination"], "구분": kind, "날짜": e["date"], "카테고리": e["category"],
                         "내용": e["description"], "금액(원)": e["amount"], "계획금액(원)": e.get("expected", e["amount"]),
                         "중요도": e["priority"], "결제수단": e.get("payment", "")})
    frame = pd.DataFrame(rows, columns=["여행지", "구분", "날짜", "카테고리", "내용", "금액(원)", "계획금액(원)", "중요도", "결제수단"])
    for column in ("여행지", "내용"):
        frame[column] = frame[column].map(lambda v: "'" + v if v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v)
    return frame.to_csv(index=False).encode("utf-8-sig")


def item_editor(store, doc, key, plan=False, existing=None, seed=None):
    base = existing or seed or {}
    a, b = date.fromisoformat(doc["start"]), date.fromisoformat(doc["end"])
    initial = date.fromisoformat(base["date"]) if base else min(max(today(), a), b)
    # Do not wrap in st.form: warnings must respond before the final save button.
    day = st.date_input("예정일" if plan else "지출일", value=initial, min_value=a, max_value=b, key=key + "date")
    cat = st.selectbox("카테고리", CATS, index=CATS.index(base.get("category", CATS[0])),
                       format_func=lambda c: ICONS[c] + " " + c, key=key + "cat")
    desc = st.text_input("지출 내용", value=base.get("description", ""), placeholder="예: 공항 → 숙소 이동", max_chars=200, key=key + "desc")
    amount = int(st.number_input("예정 금액 (₩)" if plan else "실제 금액 (₩)", min_value=1, max_value=LIMIT,
                                 value=int(base.get("amount", 10000)), step=1000, key=key + "amount"))
    priority = st.selectbox("중요도", list(PRIORITIES), index=base.get("priority", 1) - 1,
                            format_func=PRIORITIES.get, key=key + "priority")
    item = {"id": existing["id"] if existing else uuid.uuid4().hex, "date": day.isoformat(), "category": cat,
            "description": desc.strip(), "amount": amount, "priority": priority}
    accepted = True
    if not plan:
        expected = int(st.number_input("계획 금액 (₩, 미설정은 0)", min_value=0, max_value=LIMIT,
                                       value=int(base.get("expected", seed["amount"] if seed else 0)), step=1000, key=key + "expected"))
        payment = st.selectbox("결제 수단", PAYMENTS, index=PAYMENTS.index(base.get("payment", "카드")), key=key + "payment")
        item.update(expected=expected, payment=payment,
                    plan_id=existing.get("plan_id") if existing else (seed["id"] if seed else None))
        _, left, _ = stats(doc)
        after = left + (existing["amount"] if existing else 0) - amount
        warning = False
        if expected and amount > expected:
            st.warning(f"계획보다 {won(amount - expected)} 비쌉니다. 구매·이용을 다시 검토해 주세요.")
            warning = True
        if after < 0:
            st.error(f"이 지출을 확정하면 전체 예산을 {won(-after)} 초과합니다.")
            warning = True
        if warning:
            signature = hashlib.sha256(repr(item | {"id": ""}).encode()).hexdigest()[:16]
            accepted = st.checkbox("경고를 확인했고, 직접 지출을 결정합니다.", key=key + "ack" + signature)
    if st.button("계획 저장" if plan else "지출 확정 · 저장", key=key + "save", type="primary", disabled=not accepted, use_container_width=True):
        updated = copy.deepcopy(doc)
        field = "plans" if plan else "expenses"
        if existing:
            updated[field] = [item if x["id"] == existing["id"] else x for x in updated[field]]
        else:
            updated[field].append(item)
        save(store, updated)


def login(store):
    st.caption("여행 비밀코드로 어디서든 이어서 관리하세요.")
    with st.expander("🔑 기존 여행 열기", expanded=True):
        with st.form("login"):
            code = st.text_input("여행 비밀코드", type="password", max_chars=64)
            submitted = st.form_submit_button("여행 열기", use_container_width=True)
        if submitted:
            cleaned = normalize(code)
            if not re.fullmatch(r"[A-HJ-NP-Z2-9]{24}", cleaned):
                st.error("발급받은 24자리 비밀코드를 입력해 주세요. 하이픈은 생략해도 됩니다.")
            else:
                try:
                    row = store.load(code_hash(code))
                    if row:
                        validate(row["document"])
                        st.session_state.ident = code_hash(code)
                        st.session_state.code = "-".join(cleaned[i:i + 6] for i in range(0, 24, 6))
                        adopt(row)
                        st.rerun()
                    else:
                        st.error("일치하는 여행이 없습니다. 비밀코드를 확인해 주세요.")
                except (StorageError, ValueError) as exc:
                    st.error(str(exc))
    with st.expander("✨ 새 여행 만들기"):
        with st.form("new_trip"):
            destination = st.text_input("여행지", max_chars=100)
            dates = st.date_input("여행 기간", value=(today(), today() + timedelta(days=2)))
            people = int(st.number_input("여행 인원수", min_value=1, max_value=10000, value=1))
            budget = int(st.number_input("전체 예산 (₩)", min_value=1, max_value=LIMIT, value=1000000, step=10000))
            create = st.form_submit_button("여행 만들기", type="primary", use_container_width=True)
        if create:
            try:
                if len(dates) != 2:
                    raise ValueError("시작일과 종료일을 모두 선택해 주세요.")
                a, b = (d.isoformat() for d in dates)
                labels = date_list(a, b)
                doc = {"destination": destination.strip(), "start": a, "end": b, "people": people, "budget": budget,
                       "daily": allocate(budget, labels), "category": allocate(budget, CATS), "expenses": [], "plans": []}
                validate(doc)
                code = generate_code()
                st.warning("생성용 비밀코드입니다. 먼저 복사해 두세요. 생성 중 연결이 끊겨도 이 코드로 다시 확인할 수 있습니다.")
                st.code(code, language=None)
                row = store.create(code_hash(code), doc)
                st.session_state.ident = code_hash(code)
                st.session_state.code = code
                st.session_state.new_trip = True
                adopt(row)
                st.rerun()
            except (StorageError, ValueError) as exc:
                st.error(str(exc))
    st.stop()


def budget_settings(store, doc):
    version = str(st.session_state.version)
    destination = st.text_input("여행지", value=doc["destination"], max_chars=100, key="dest" + version)
    dates = st.date_input("여행 기간", value=(date.fromisoformat(doc["start"]), date.fromisoformat(doc["end"])), key="dates" + version)
    people = int(st.number_input("여행 인원수", min_value=1, max_value=10000, value=doc["people"], key="people" + version))
    budget = int(st.number_input("전체 예산 (₩)", min_value=1, max_value=LIMIT, value=doc["budget"], step=10000, key="budget" + version))
    st.caption(f"1인당 예산 {won(budget // people)} · 원 미만 버림")
    if len(dates) != 2:
        st.info("시작일과 종료일을 모두 선택해 주세요.")
        return
    a, b = (d.isoformat() for d in dates)
    try:
        labels = date_list(a, b)
    except ValueError as exc:
        st.error(str(exc))
        return
    auto = st.checkbox("날짜·카테고리 예산 균등 재배분", key="auto" + version)
    daily = allocate(budget, labels) if auto else {day: doc["daily"].get(day, 0) for day in labels}
    category = allocate(budget, CATS) if auto else doc["category"]
    suffix = version + str(budget) + a + b + str(auto)
    config = {"예산(원)": st.column_config.NumberColumn(min_value=0, max_value=LIMIT, step=1, required=True)}
    st.caption("날짜별 합계와 카테고리별 합계가 각각 전체 예산과 같아야 합니다. 금액 셀을 눌러 수정하세요.")
    day_frame = st.data_editor(pd.DataFrame([{"날짜": k, "예산(원)": v} for k, v in daily.items()]),
                               disabled=["날짜"], hide_index=True, column_config=config, use_container_width=True, key="daily" + suffix)
    cat_frame = st.data_editor(pd.DataFrame([{"카테고리": k, "예산(원)": v} for k, v in category.items()]),
                               disabled=["카테고리"], hide_index=True, column_config=config, use_container_width=True, key="cat" + suffix)
    values = list(day_frame["예산(원)"]) + list(cat_frame["예산(원)"])
    if not all(pd.notna(v) and float(v).is_integer() and 0 <= v <= LIMIT for v in values):
        st.error("빈칸·소수·음수 없이 입력해 주세요.")
        return
    daily = dict(zip(day_frame["날짜"], map(int, day_frame["예산(원)"])))
    category = dict(zip(cat_frame["카테고리"], map(int, cat_frame["예산(원)"])))
    st.caption(f"날짜 합계 {won(sum(daily.values()))} · 카테고리 합계 {won(sum(category.values()))} · 전체 {won(budget)}")
    equal = sum(daily.values()) == sum(category.values()) == budget
    if not equal:
        st.warning("각 배분 합계를 전체 예산에 맞춰 주세요.")
    if st.button("여행 정보 · 예산 저장", type="primary", use_container_width=True, disabled=not equal):
        updated = copy.deepcopy(doc)
        updated.update(destination=destination.strip(), start=a, end=b, people=people, budget=budget, daily=daily, category=category)
        save(store, updated)


def analysis(doc):
    spent, _, _ = stats(doc)
    rows = []
    for cat in CATS:
        actual = sum(e["amount"] for e in doc["expenses"] if e["category"] == cat)
        rows.append({"카테고리": cat, "계획(원)": doc["category"][cat], "실제(원)": actual,
                     "잔여(원)": doc["category"][cat] - actual, "지출 비율(%)": round(actual / spent * 100, 1) if spent else 0.0})
    frame = pd.DataFrame(rows).set_index("카테고리")
    st.bar_chart(frame[["계획(원)", "실제(원)"]], color=["#A4C9C1", "#137C66"], stack=False)
    st.dataframe(frame, use_container_width=True)
    if spent:
        highest = max(rows, key=lambda r: r["실제(원)"])
        st.info(f"가장 큰 지출: {ICONS[highest['카테고리']]} {highest['카테고리']} · {won(highest['실제(원)'])} · {highest['지출 비율(%)']}%")
    daily_rows = []
    for day in date_list(doc["start"], doc["end"]):
        actual = sum(e["amount"] for e in doc["expenses"] if e["date"] == day)
        daily_rows.append({"날짜": day, "계획(원)": doc["daily"][day], "실제(원)": actual, "잔여(원)": doc["daily"][day] - actual})
    st.dataframe(pd.DataFrame(daily_rows).set_index("날짜"), use_container_width=True)
    st.caption("잔여가 음수이면 해당 배정 예산을 초과했습니다. 일별 배정 잔액과 전체 잔여 예산은 별도입니다.")


def sharing():
    st.warning("비밀코드 소지자는 여행을 조회·수정할 수 있습니다. 분실 시 복구할 수 없으니 안전하게 보관하세요.")
    if st.checkbox("비밀코드 표시"):
        st.code(st.session_state.code, language=None)
    st.caption("QR에는 앱 주소만 포함합니다. 비밀코드는 별도로 전달하세요. 민감한 개인정보는 입력하지 마세요.")
    address = str(st.secrets.get("APP_URL", "")).strip()
    try:
        parsed = urlsplit(address)
        valid = parsed.scheme == "https" and bool(parsed.hostname) and not any([parsed.query, parsed.fragment, parsed.username, parsed.password]) and not any(c.isspace() for c in address)
    except ValueError:
        valid = False
    if valid:
        buffer = io.BytesIO()
        qrcode.make(address).save(buffer, format="PNG")
        st.image(buffer.getvalue(), width=190, caption="여행경비 최적화 접속 QR")
        st.download_button("QR코드 PNG 저장", buffer.getvalue(), "travel_app_qr.png", "image/png", use_container_width=True)
        st.link_button("앱 주소 열기", address, use_container_width=True)
    else:
        st.info('Streamlit Secrets에 APP_URL = "https://내앱.streamlit.app"을 설정하면 QR코드가 표시됩니다.')
    if st.button("여행 닫기 · 데이터 유지", use_container_width=True):
        st.session_state.clear()
        st.rerun()


def main():
    st.set_page_config(page_title="여행경비 최적화", page_icon="🧳", layout="centered")
    st.markdown('''<style>
    .stApp{background:#F6F9F5}.block-container{max-width:780px;padding-top:1.2rem;padding-bottom:2rem}
    h1{font-size:1.8rem!important;letter-spacing:-1px}button,input{min-height:44px!important}
    button{border-radius:12px!important}[data-testid="stExpander"]{border-radius:15px;background:#FFFFFFA0}
    .summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin:10px 0}
    .tile{border-radius:15px;background:#E5F1EA;padding:14px 9px;border:1px solid #C9DDCF;min-width:0}
    .tile small{color:#42634F;font-size:12px}.tile strong{display:block;font-size:clamp(14px,4.2vw,24px);color:#193E2D;overflow-wrap:anywhere;margin-top:4px}
    .tile.danger{background:#FFEAE7;border-color:#F0B8B1}.tile.danger strong{color:#AD2E22}
    @media(max-width:600px){.block-container{padding-left:12px;padding-right:12px}.summary{gap:5px}.tile{padding:11px 6px}}
    </style>''', unsafe_allow_html=True)
    st.title("🧳 여행경비 최적화")
    try:
        url = str(st.secrets["SUPABASE_URL"]).strip()
        key = str(st.secrets["SUPABASE_SERVICE_ROLE_KEY"]).strip()
        if not url.startswith("https://") or not key:
            raise ValueError("설정 누락")
    except (KeyError, FileNotFoundError, ValueError):
        st.info("최초 설정이 필요합니다. app.py 맨 위 주석의 SQL을 Supabase에서 실행하고 Streamlit Secrets에 SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY를 추가하세요.")
        st.stop()
    store = Store(url, key)
    if "ident" not in st.session_state:
        login(store)
    doc = st.session_state.doc
    spent, left, count = stats(doc)
    if "message" in st.session_state:
        st.toast(st.session_state.pop("message"))
    if st.session_state.get("new_trip"):
        st.success("여행 생성 완료! 아래 비밀코드를 반드시 복사해 보관하세요.")
        st.code(st.session_state.code, language=None)
        if st.button("비밀코드를 보관했습니다", use_container_width=True):
            st.session_state.new_trip = False
            st.rerun()
    st.caption(f"{doc['destination']} · {doc['start']} ~ {doc['end']} · {doc['people']}명")
    st.markdown(f'''<div class="summary"><div class="tile"><small>전체 예산</small><strong>{won(doc['budget'])}</strong></div>
    <div class="tile"><small>총지출</small><strong>{won(spent)}</strong></div>
    <div class="tile {'danger' if left < 0 else ''}"><small>잔여 예산</small><strong>{won(left)}</strong></div></div>''', unsafe_allow_html=True)
    st.progress(min(1.0, spent / doc["budget"]), text=f"예산 사용률 {spent / doc['budget']:.1%}")
    st.caption(f"1인당 예산 {won(doc['budget'] // doc['people'])} · 1인당 지출 {won(spent // doc['people'])} · 원 미만 버림")
    if count:
        st.caption(f"남은 {count}일 · 하루 사용 가능 {won(max(0, left) // count)} · 한국시간, 오늘 포함")
    else:
        st.caption("여행 종료 · 전체 내역과 CSV를 확인하세요.")
    if left < 0:
        st.error(f"🚨 예산 {won(-left)} 초과! 추가 지출을 신중하게 결정하세요.")
    plans = pending(doc)
    shortfall = sum(p["amount"] for p in plans) - left
    if shortfall > 0 and plans:
        st.warning(f"미결제 계획까지 사용하면 {won(shortfall)} 부족합니다.")
        with st.expander("💡 절약 후보 · 중요도 3 → 2"):
            candidates = sorted([p for p in plans if p["priority"] in (3, 2)], key=lambda p: (-p["priority"], -p["amount"]))
            for p in candidates:
                st.write(f"{p['priority']} · {p['description']} · {won(p['amount'])}")
            if candidates:
                st.caption(f"전체 후보 생략 시 최대 {won(sum(p['amount'] for p in candidates))} 절약 가능. 취소 여부는 직접 결정하세요.")
            else:
                st.info("중요도 2·3 계획이 없습니다. 필수 지출과 전체 예산을 검토하세요.")
            st.caption("이미 결제한 지출은 절약 후보 금액에 포함하지 않습니다.")
    elif left <= doc["budget"] * 0.2:
        st.warning("잔여 예산 20% 이하 · 중요도 3 지출부터 검토하세요.")

    with st.expander("➕ 지출 등록"):
        choices = {"direct": None} | {p["id"]: p for p in plans}
        pick = st.selectbox("직접 입력 / 계획 결제", list(choices),
                            format_func=lambda k: "직접 입력" if k == "direct" else label(choices[k]), key="add_choice")
        item_editor(store, doc, f"add_{st.session_state.version}_{pick}_", seed=choices[pick])

    with st.expander(f"📝 미결제 계획 · {len(plans)}건"):
        st.caption("계획은 잔여 예산에서 차감하지 않습니다. 지출 등록에서 계획을 선택해 결제를 확정하세요.")
        mode = st.radio("계획 작업", ["새 계획", "수정·삭제"], horizontal=True)
        if mode == "새 계획":
            item_editor(store, doc, f"newplan_{st.session_state.version}_", plan=True)
        elif not plans:
            st.info("미결제 계획이 없습니다.")
        else:
            by_id = {p["id"]: p for p in plans}
            pid = st.selectbox("계획 선택", list(by_id), format_func=lambda k: label(by_id[k]))
            item_editor(store, doc, f"editplan_{pid}_{st.session_state.version}_", plan=True, existing=by_id[pid])
            checked = st.checkbox("이 계획을 삭제합니다.", key="delplan" + pid)
            if st.button("계획 삭제", disabled=not checked, use_container_width=True):
                updated = copy.deepcopy(doc)
                updated["plans"] = [p for p in updated["plans"] if p["id"] != pid]
                save(store, updated)

    with st.expander("📊 계획 대비 실제 · 카테고리 / 날짜"):
        analysis(doc)

    with st.expander(f"📒 지출 내역 · {len(doc['expenses'])}건 · CSV"):
        st.download_button("전체 CSV 다운로드", make_csv(doc), "travel_expenses.csv", "text/csv", use_container_width=True)
        st.caption("CSV에는 실제 지출과 아직 결제하지 않은 계획이 구분되어 포함됩니다.")
        if not doc["expenses"]:
            st.info("등록한 지출이 없습니다.")
        else:
            table = [{"날짜": e["date"], "카테고리": e["category"], "내용": e["description"], "금액(원)": e["amount"],
                      "계획금액(원)": e["expected"], "중요도": e["priority"], "결제수단": e["payment"]}
                     for e in sorted(doc["expenses"], key=lambda e: e["date"])]
            st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
            by_id = {e["id"]: e for e in doc["expenses"]}
            eid = st.selectbox("수정할 지출", list(by_id), format_func=lambda k: label(by_id[k]))
            item_editor(store, doc, f"edit_{eid}_{st.session_state.version}_", existing=by_id[eid])
            checked = st.checkbox("이 지출을 삭제합니다. 연결된 계획은 미결제로 복원됩니다.", key="delexp" + eid)
            if st.button("지출 삭제", disabled=not checked, use_container_width=True):
                updated = copy.deepcopy(doc)
                updated["expenses"] = [e for e in updated["expenses"] if e["id"] != eid]
                save(store, updated)

    with st.expander("⚙️ 여행 정보 · 날짜별 / 카테고리별 예산"):
        budget_settings(store, doc)
    with st.expander("🔐 비밀코드 · QR 공유"):
        sharing()
    if st.button("↻ 새로 불러오기", use_container_width=True):
        try:
            row = store.load(st.session_state.ident)
            if row:
                validate(row["document"])
                adopt(row)
                st.rerun()
            else:
                st.error("여행 데이터를 찾을 수 없습니다.")
        except (StorageError, ValueError) as exc:
            st.error(str(exc))
    st.caption("저장 즉시 현재 화면에 반영 · 다른 기기의 변경은 새로 불러오기 · 데이터는 Supabase에 보관")


if __name__ == "__main__":
    main()

"""여행경비 최적화 | Python 3.11/3.12, Streamlit Community Cloud

배포: 이 파일과 requirements.txt를 GitHub에 업로드하고 Cloud에서 app.py 실행.
Supabase SQL Editor에서 아래 SQL을 한 번 실행하고 Cloud의 Settings > Secrets 설정.
서비스 역할 키는 서버 Secrets에만 보관하고 GitHub/QR/브라우저에 노출하지 마세요.

-- Supabase 초기 SQL (여행 데이터 테이블 전용)
create table if not exists public.travel_budgets (
    id text primary key,
    document jsonb not null,
    version integer not null default 1
);
alter table public.travel_budgets enable row level security;
revoke all on public.travel_budgets from anon, authenticated;
grant select, insert, update on public.travel_budgets to service_role;
-- 공개 접근 정책을 만들지 마세요. 이 앱만 service_role로 접근합니다.

# Streamlit Cloud Secrets (TOML)
SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "YOUR-SERVER-SERVICE-ROLE-KEY"
APP_URL = "https://YOUR-APP.streamlit.app"

실행: streamlit run app.py
APP_URL은 선택 항목이며, 설정하면 앱 내 QR코드가 표시됩니다.
DB에는 여행 비밀코드 원문 대신 SHA-256 해시만 저장합니다.
코드는 소지자에게 읽기/수정 권한을 주는 비밀번호입니다. 분실하면 복구할 수 없습니다.
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

import pandas as pd
import qrcode
import requests
import streamlit as st

CATEGORIES = ["교통비", "숙박비", "식비", "관광·체험비", "쇼핑비", "기타"]
PAYMENTS = ["카드", "현금", "계좌이체", "간편결제", "기타"]
PRIORITIES = {1: "1 · 꼭 필요한 지출", 2: "2 · 상황에 따라 필요한 지출", 3: "3 · 줄이거나 생략 가능한 지출"}
MAX_MONEY = 1_000_000_000_000
KST = timezone(timedelta(hours=9))


def today():
    return datetime.now(KST).date()


def won(value):
    return f"₩{int(value):,}"


def days(start, end):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def split_budget(total, labels):
    base, extra = divmod(int(total), len(labels))
    return {label: base + (i < extra) for i, label in enumerate(labels)}


def normalize_code(code):
    return code.strip().replace("-", "").replace(" ", "").upper()


def code_id(code):
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def new_code():
    # 120 bits of randomness, unambiguous alphabet, not a guessable numeric PIN.
    raw = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(24))
    return "-".join(raw[i:i + 6] for i in range(0, 24, 6))


def money_ok(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_MONEY


def validate(doc):
    if not str(doc["destination"]).strip() or len(doc["destination"]) > 100:
        raise ValueError("여행지는 1~100자로 입력해 주세요.")
    dates = days(doc["start"], doc["end"])
    if not 1 <= len(dates) <= 366:
        raise ValueError("여행 기간은 1~366일이어야 합니다.")
    if not 1 <= int(doc["people"]) <= 10000:
        raise ValueError("여행 인원은 1~10,000명이어야 합니다.")
    if not money_ok(doc["budget"]) or doc["budget"] == 0:
        raise ValueError("전체 예산은 1원 이상이어야 합니다.")
    for key, labels in [("daily", dates), ("category", CATEGORIES)]:
        alloc = doc[key]
        if set(alloc) != set(labels) or not all(money_ok(v) for v in alloc.values()):
            raise ValueError("예산 배분 값이 잘못되었습니다. 음수·소수는 입력할 수 없습니다.")
        if sum(alloc.values()) != doc["budget"]:
            raise ValueError("날짜별 합계와 카테고리별 합계는 각각 전체 예산과 일치해야 합니다.")
    for kind in ["expenses", "plans"]:
        ids = set()
        for item in doc[kind]:
            if item["id"] in ids:
                raise ValueError("중복된 항목입니다.")
            ids.add(item["id"])
            if item["date"] not in dates:
                raise ValueError("기존 지출·계획이 여행 기간 밖에 있습니다. 먼저 날짜를 수정해 주세요.")
            if item["category"] not in CATEGORIES or item["priority"] not in PRIORITIES:
                raise ValueError("카테고리 또는 중요도가 올바르지 않습니다.")
            if not item["description"].strip() or len(item["description"]) > 200:
                raise ValueError("내용은 1~200자로 입력해 주세요.")
            if not money_ok(item["amount"]) or item["amount"] == 0:
                raise ValueError("항목 금액은 1원 이상의 정수여야 합니다.")
            if kind == "expenses" and not money_ok(item["expected"]):
                raise ValueError("비교용 계획 금액이 올바르지 않습니다.")
    plan_ids = {p["id"] for p in doc["plans"]}
    links = [e["plan_id"] for e in doc["expenses"] if e.get("plan_id")]
    if len(links) != len(set(links)) or any(p not in plan_ids for p in links):
        raise ValueError("계획과 지출의 연결 정보가 잘못되었습니다.")


class StoreError(Exception):
    pass


class Store:
    def __init__(self, url, key):
        self.url = url.rstrip("/") + "/rest/v1/travel_budgets"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json", "Prefer": "return=representation"}

    def request(self, method, params=None, data=None):
        try:
            response = requests.request(method, self.url, headers=self.headers,
                                        params=params, json=data, timeout=(8, 25))
        except requests.RequestException:
            raise StoreError("데이터베이스 연결이 불안정합니다. 다시 불러와 저장 여부를 확인해 주세요.") from None
        if not response.ok:
            # Do not expose server internals, row identifiers, or service credentials.
            if response.status_code == 409:
                raise StoreError("이미 존재하는 데이터 또는 중복 요청입니다. 다시 불러와 확인해 주세요.")
            raise StoreError(f"데이터베이스 요청 실패 (HTTP {response.status_code}). Supabase 테이블·Secrets·프로젝트 상태를 확인해 주세요.")
        try:
            return response.json()
        except ValueError:
            raise StoreError("데이터베이스 응답을 읽지 못했습니다. 다시 불러와 확인해 주세요.") from None

    def load(self, ident):
        rows = self.request("GET", {"id": f"eq.{ident}", "select": "document,version", "limit": "1"})
        return rows[0] if rows else None

    def create(self, ident, doc):
        validate(doc)
        rows = self.request("POST", data={"id": ident, "document": doc, "version": 1})
        if len(rows) != 1:
            raise StoreError("생성 결과를 확인하지 못했습니다. 비밀코드로 다시 접속해 주세요.")
        return rows[0]

    def save(self, ident, version, doc):
        validate(doc)
        rows = self.request("PATCH", {"id": f"eq.{ident}", "version": f"eq.{version}"},
                            {"document": doc, "version": version + 1})
        if len(rows) != 1:
            raise StoreError("다른 기기에서 먼저 변경했습니다. 상단 '최신 정보 불러오기' 후 다시 입력해 주세요. 현재 입력은 저장되지 않았습니다.")
        return rows[0]


def adopt(row):
    st.session_state.doc = row["document"]
    st.session_state.version = row["version"]


def save(doc):
    try:
        row = store.save(st.session_state.ident, st.session_state.version, doc)
        adopt(row)
        st.session_state.flash = "안전하게 저장했습니다."
        st.rerun()
    except (StoreError, ValueError) as exc:
        st.error(str(exc))


def outstanding(doc):
    paid = {e.get("plan_id") for e in doc["expenses"]}
    return [p for p in doc["plans"] if p["id"] not in paid]


def totals(doc):
    spent = sum(e["amount"] for e in doc["expenses"])
    remaining = doc["budget"] - spent
    start, end = date.fromisoformat(doc["start"]), date.fromisoformat(doc["end"])
    count = max(0, (end - max(today(), start)).days + 1)
    return spent, remaining, count


def item_label(item):
    return f'{item["date"]} · {item["description"]} · {won(item["amount"])}'


def item_form(key, doc, existing=None, plan=False, seed=None):
    """Not inside st.form: price warnings must update BEFORE confirmation."""
    base = existing or seed or {}
    start, end = date.fromisoformat(doc["start"]), date.fromisoformat(doc["end"])
    default_date = min(max(today(), start), end)
    expense_date = st.date_input("예정일" if plan else "지출일", value=date.fromisoformat(base["date"]) if base else default_date,
                                 min_value=start, max_value=end, key=key + "date")
    category = st.selectbox("카테고리", CATEGORIES, index=CATEGORIES.index(base.get("category", CATEGORIES[0])), key=key + "cat")
    description = st.text_input("구체적인 내용", value=base.get("description", ""), max_chars=200, key=key + "desc")
    priority = st.selectbox("중요도", list(PRIORITIES), index=base.get("priority", 1) - 1,
                            format_func=PRIORITIES.get, key=key + "pri")
    amount = int(st.number_input("예정 금액 (₩)" if plan else "실제 금액 (₩)", min_value=1, max_value=MAX_MONEY,
                                 value=int(base.get("amount", 10000)), step=1000, key=key + "amount"))
    item = {"id": existing["id"] if existing else uuid.uuid4().hex, "date": expense_date.isoformat(),
            "category": category, "description": description.strip(), "priority": priority, "amount": amount}
    if not plan:
        expected_default = base.get("expected", seed["amount"] if seed else 0)
        expected = int(st.number_input("비교용 계획 금액 (₩, 0 = 미설정)", min_value=0, max_value=MAX_MONEY,
                                       value=int(expected_default), step=1000, key=key + "expected"))
        payment = st.selectbox("결제 수단", PAYMENTS, index=PAYMENTS.index(base.get("payment", PAYMENTS[0])), key=key + "pay")
        item.update(expected=expected, payment=payment, plan_id=existing.get("plan_id") if existing else (seed["id"] if seed else None))
        spent, _, _ = totals(doc)
        after = doc["budget"] - spent + (existing["amount"] if existing else 0) - amount
        warnings = []
        if expected and amount > expected:
            warnings.append(f"계획보다 {won(amount - expected)} 비쌉니다. 구매·이용을 다시 검토해 주세요.")
        if after < 0:
            warnings.append(f"등록하면 전체 예산을 {won(-after)} 초과합니다.")
        if warnings:
            for warning in warnings:
                st.warning(warning)
            # A changed price/category/date invalidates the previous acknowledgement.
            signature = hashlib.sha256(repr(item | {"id": ""}).encode()).hexdigest()[:16]
            accepted = st.checkbox("경고를 확인했으며 직접 지출 등록을 결정합니다.", key=key + "ack" + signature)
        else:
            accepted = True
    else:
        accepted = True
    if st.button("계획 저장" if plan else "지출 확정 · 저장", key=key + "submit", type="primary", disabled=not accepted,
                 use_container_width=True):
        if not description.strip():
            st.error("지출 내용을 입력해 주세요.")
            return
        updated = copy.deepcopy(doc)
        field = "plans" if plan else "expenses"
        if existing:
            updated[field] = [item if x["id"] == existing["id"] else x for x in updated[field]]
        else:
            updated[field].append(item)
        save(updated)


def csv_bytes(doc):
    paid = {e.get("plan_id") for e in doc["expenses"]}
    rows = []
    for kind, items in [("실제 지출", doc["expenses"]), ("미결제 계획", [p for p in doc["plans"] if p["id"] not in paid])]:
        for item in items:
            rows.append({"여행지": doc["destination"], "구분": kind, "날짜": item["date"], "카테고리": item["category"],
                         "내용": item["description"], "중요도": item["priority"], "금액(원)": item["amount"],
                         "계획금액(원)": item.get("expected", item["amount"]), "결제수단": item.get("payment", "")})
    cols = ["여행지", "구분", "날짜", "카테고리", "내용", "중요도", "금액(원)", "계획금액(원)", "결제수단"]
    frame = pd.DataFrame(rows, columns=cols)
    # Prevent spreadsheet formula execution in user-controlled text cells.
    for column in ["여행지", "내용"]:
        frame[column] = frame[column].map(lambda v: "'" + v if v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v)
    return frame.to_csv(index=False).encode("utf-8-sig")


def main():
    global store
    st.set_page_config(page_title="여행경비 최적화", page_icon="🧳", layout="centered")
    st.markdown("""<style>
    .block-container {max-width:760px;padding-top:1.2rem;padding-bottom:2rem;}
    h1 {font-size:1.65rem!important;} h2 {font-size:1.25rem!important;}
    button, input {min-height:44px!important;} [data-testid='stExpander'] {border-radius:14px;}
    .hero {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0;}
    .tile {background:#edf7f5;border:1px solid #c9e7e1;border-radius:14px;padding:12px 8px;min-width:0;}
    .tile small {font-size:12px;color:#315950;} .tile strong {display:block;font-size:clamp(14px,4vw,23px);overflow-wrap:anywhere;color:#123f35;}
    .tile.over {background:#fff0ef;border-color:#efb1aa;} .tile.over strong {color:#a52218;}
    @media(max-width:600px){.block-container{padding-left:12px;padding-right:12px;}.hero{gap:5px;}.tile{padding:10px 6px;}}
    </style>""", unsafe_allow_html=True)
    st.title("🧳 여행경비 최적화")
    try:
        url = str(st.secrets["SUPABASE_URL"]).strip()
        key = str(st.secrets["SUPABASE_SERVICE_ROLE_KEY"]).strip()
        if not url.startswith("https://") or not key:
            raise ValueError("configuration")
    except (KeyError, FileNotFoundError, ValueError):
        st.info("최초 설정이 필요합니다. app.py 맨 위의 SQL을 Supabase에서 실행하고, Streamlit Cloud Secrets에 SUPABASE_URL과 SUPABASE_SERVICE_ROLE_KEY를 추가해 주세요.")
        st.stop()
    store = Store(url, key)
    if "ident" not in st.session_state:
        st.caption("비밀코드로 어디서든 같은 여행을 이어가세요. 코드 소지자는 데이터를 수정할 수 있습니다.")
        with st.expander("🔑 기존 여행 열기", expanded=True):
            with st.form("login"):
                code = st.text_input("여행 비밀코드", type="password", max_chars=64)
                open_trip = st.form_submit_button("여행 불러오기", use_container_width=True)
            if open_trip:
                cleaned = normalize_code(code)
                if not re.fullmatch(r"[A-HJ-NP-Z2-9]{24}", cleaned):
                    st.error("발급받은 24자리 비밀코드를 확인해 주세요. 하이픈은 생략해도 됩니다.")
                else:
                    try:
                        row = store.load(code_id(code))
                        if row is None:
                            st.error("일치하는 여행이 없습니다. 코드를 확인해 주세요.")
                        else:
                            validate(row["document"])
                            st.session_state.ident = code_id(code)
                            st.session_state.code = "-".join(cleaned[i:i + 6] for i in range(0, 24, 6))
                            adopt(row)
                            st.rerun()
                    except (StoreError, ValueError) as exc:
                        st.error(str(exc))
        with st.expander("✨ 새 여행 만들기"):
            with st.form("create"):
                dest = st.text_input("여행지", max_chars=100)
                selected = st.date_input("여행 기간", value=(today(), today() + timedelta(days=2)))
                people = st.number_input("인원수", min_value=1, max_value=10000, value=1)
                budget = st.number_input("전체 예산 (₩)", min_value=1, max_value=MAX_MONEY, value=1000000, step=10000)
                create = st.form_submit_button("새 여행 생성", type="primary", use_container_width=True)
            if create:
                if len(selected) != 2 or not dest.strip():
                    st.error("여행지와 시작·종료일을 모두 입력해 주세요.")
                else:
                    a, b = (d.isoformat() for d in selected)
                    if len(days(a, b)) > 366:
                        st.error("여행 기간은 최대 366일입니다.")
                    else:
                        doc = {"destination": dest.strip(), "start": a, "end": b, "people": int(people), "budget": int(budget),
                               "daily": split_budget(budget, days(a, b)), "category": split_budget(budget, CATEGORIES),
                               "expenses": [], "plans": []}
                        code = new_code()
                        # Keep recovery code visible if an ambiguous network failure happens.
                        st.warning("생성용 비밀코드입니다. 먼저 안전한 곳에 복사해 주세요. 분실 시 복구할 수 없습니다.")
                        st.code(code, language=None)
                        try:
                            row = store.create(code_id(code), doc)
                            st.session_state.ident = code_id(code)
                            st.session_state.code = code
                            adopt(row)
                            st.session_state.flash = "여행을 생성했습니다. 아래 '접속·공유'에서 비밀코드를 반드시 보관해 주세요."
                            st.rerun()
                        except (StoreError, ValueError) as exc:
                            st.error(str(exc))
        st.stop()

    doc = st.session_state.doc
    spent, remaining, count = totals(doc)
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))
    st.caption(f'{doc["destination"]} · {doc["start"]} ~ {doc["end"]} · {doc["people"]}명')
    st.markdown(f'''<div class="hero"><div class="tile"><small>전체 예산</small><strong>{won(doc['budget'])}</strong></div>
    <div class="tile"><small>총지출</small><strong>{won(spent)}</strong></div>
    <div class="tile {'over' if remaining < 0 else ''}"><small>남은 예산</small><strong>{won(remaining)}</strong></div></div>''', unsafe_allow_html=True)
    st.progress(min(spent / doc["budget"], 1.0), text=f"예산 사용률 {spent / doc['budget']:.1%}")
    st.caption(f"1인당 예산 {won(doc['budget'] // doc['people'])} · 1인당 지출 {won(spent // doc['people'])} (원 미만 버림)")
    if count:
        st.caption(f"남은 {count}일 · 하루 사용 가능 {won(max(0, remaining) // count)} (한국시간 기준, 오늘 포함)")
    else:
        st.caption("여행 기간이 끝났습니다. 전체 내역과 CSV를 확인해 보세요.")
    if remaining < 0:
        st.error(f"🚨 전체 예산을 {won(-remaining)} 초과했습니다. 추가 지출을 신중하게 결정해 주세요.")
    if st.button("↻ 최신 정보 불러오기", use_container_width=True):
        try:
            row = store.load(st.session_state.ident)
            if row:
                validate(row["document"])
                adopt(row)
                st.rerun()
            else:
                st.error("여행 데이터를 찾을 수 없습니다.")
        except (StoreError, ValueError) as exc:
            st.error(str(exc))
    st.caption("현재 세션은 저장 즉시 갱신됩니다. 다른 스마트폰의 변경은 위 버튼으로 반영하세요.")

    pending = outstanding(doc)
    forecast = sum(p["amount"] for p in pending)
    shortfall = forecast - remaining
    if shortfall > 0 and pending:
        st.warning(f"미결제 계획까지 지출하면 {won(shortfall)} 부족합니다. 중요도 3 → 2 순서로 검토하세요.")
        with st.expander("💡 절약 후보 확인", expanded=False):
            st.caption("이미 결제한 금액은 절약 가능액에 포함하지 않습니다. 자동 취소·차단은 하지 않습니다.")
            candidates = sorted([p for p in pending if p["priority"] in (3, 2)], key=lambda p: (-p["priority"], -p["amount"]))
            if candidates:
                for p in candidates:
                    st.write(f"중요도 {p['priority']} · {p['description']} · {won(p['amount'])}")
                can_save = sum(p["amount"] for p in candidates)
                st.caption(f"전체 후보를 생략할 때 최대 {won(can_save)} 절약 가능. 실제 절약 여부는 직접 결정하세요.")
            else:
                st.info("줄일 수 있는 중요도 2·3 계획이 없습니다. 필수 지출 또는 전체 예산을 다시 검토해 주세요.")
    elif remaining <= doc["budget"] * 0.2:
        st.warning("남은 예산이 전체의 20% 이하입니다. 신규 지출은 중요도 3부터 다시 검토하세요.")

    with st.expander("➕ 지출 입력", expanded=False):
        choices = {"direct": None} | {p["id"]: p for p in pending}
        pick = st.selectbox("입력 방식", list(choices), format_func=lambda k: "직접 입력" if k == "direct" else "계획 결제: " + item_label(choices[k]), key="add_choice")
        item_form(f"add_{st.session_state.version}_{pick}_", doc, seed=choices[pick])

    with st.expander("📝 미결제 계획 · 수정 · 삭제"):
        st.caption("계획은 실제 잔여 예산에서 차감하지 않으며, 결제 확정 시 한 번만 반영됩니다.")
        mode = st.radio("계획 작업", ["새 계획", "기존 계획 관리"], horizontal=True)
        if mode == "새 계획":
            item_form(f"plan_new_{st.session_state.version}_", doc, plan=True)
        elif not pending:
            st.info("미결제 계획이 없습니다. 결제 완료 계획은 연결된 지출 삭제 시 복원됩니다.")
        else:
            by_id = {p["id"]: p for p in pending}
            pid = st.selectbox("수정할 계획", list(by_id), format_func=lambda k: item_label(by_id[k]))
            item_form(f"plan_edit_{pid}_{st.session_state.version}_", doc, existing=by_id[pid], plan=True)
            confirm = st.checkbox("이 계획을 삭제하겠습니다.", key="delplan" + pid)
            if st.button("선택 계획 삭제", disabled=not confirm, use_container_width=True):
                updated = copy.deepcopy(doc)
                updated["plans"] = [p for p in updated["plans"] if p["id"] != pid]
                save(updated)

    with st.expander("📊 계획 대비 실제 · 카테고리 / 날짜 분석"):
        rows = []
        for category in CATEGORIES:
            actual = sum(e["amount"] for e in doc["expenses"] if e["category"] == category)
            planned = doc["category"][category]
            rows.append({"카테고리": category, "계획(원)": planned, "실제(원)": actual,
                         "잔여(원)": planned - actual, "지출 비율(%)": round(actual / spent * 100, 1) if spent else 0.0})
        frame = pd.DataFrame(rows).set_index("카테고리")
        st.dataframe(frame, use_container_width=True)
        st.bar_chart(frame[["계획(원)", "실제(원)"]], color=["#a4c9c1", "#137c66"], stack=False)
        if spent:
            largest = max(rows, key=lambda r: r["실제(원)"])
            st.info(f"가장 많이 사용한 항목: {largest['카테고리']} · {won(largest['실제(원)'])} ({largest['지출 비율(%)']}%)")
        daily_rows = []
        for day in days(doc["start"], doc["end"]):
            actual = sum(e["amount"] for e in doc["expenses"] if e["date"] == day)
            daily_rows.append({"날짜": day, "계획(원)": doc["daily"][day], "실제(원)": actual,
                               "잔여(원)": doc["daily"][day] - actual})
        st.dataframe(pd.DataFrame(daily_rows).set_index("날짜"), use_container_width=True)
        st.caption("잔여(원)가 음수인 항목은 배정 예산을 초과했습니다. 일별 잔여와 전체 남은 예산은 별도입니다.")

    with st.expander("📒 전체 지출 내역 · 수정 · 삭제 · CSV"):
        st.download_button("CSV 내려받기 (실제 지출 + 미결제 계획)", csv_bytes(doc), file_name="travel_expenses.csv",
                           mime="text/csv", use_container_width=True)
        if not doc["expenses"]:
            st.info("등록된 지출이 없습니다.")
        else:
            table = [{"날짜": e["date"], "카테고리": e["category"], "내용": e["description"], "금액(원)": e["amount"],
                      "계획금액(원)": e["expected"], "중요도": e["priority"], "결제수단": e["payment"]}
                     for e in sorted(doc["expenses"], key=lambda e: e["date"])]
            st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
            by_id = {e["id"]: e for e in doc["expenses"]}
            eid = st.selectbox("수정할 지출", list(by_id), format_func=lambda k: item_label(by_id[k]))
            item_form(f"edit_{eid}_{st.session_state.version}_", doc, existing=by_id[eid])
            confirm = st.checkbox("이 지출을 삭제하겠습니다. 연결된 계획은 미결제로 복원됩니다.", key="delexp" + eid)
            if st.button("선택 지출 삭제", disabled=not confirm, use_container_width=True):
                updated = copy.deepcopy(doc)
                updated["expenses"] = [e for e in updated["expenses"] if e["id"] != eid]
                save(updated)

    with st.expander("⚙️ 여행 정보 · 예산 배분 설정"):
        st.caption("날짜별 합계와 카테고리별 합계가 각각 전체 예산과 일치해야 저장됩니다. 최초 생성 시 균등 배분됩니다.")
        suffix = str(st.session_state.version)
        dest = st.text_input("여행지", value=doc["destination"], max_chars=100, key="dest" + suffix)
        selected = st.date_input("여행 기간", value=(date.fromisoformat(doc["start"]), date.fromisoformat(doc["end"])), key="range" + suffix)
        people = int(st.number_input("여행 인원수", min_value=1, max_value=10000, value=doc["people"], key="people" + suffix))
        budget = int(st.number_input("전체 여행 예산 (₩)", min_value=1, max_value=MAX_MONEY, value=doc["budget"], step=10000, key="budget" + suffix))
        st.caption(f"변경 후 1인당 예산: {won(budget // people)} (원 미만 버림)")
        if len(selected) != 2:
            st.info("시작일과 종료일을 모두 선택해 주세요.")
        elif not 1 <= (selected[1] - selected[0]).days + 1 <= 366:
            st.error("여행 기간은 1~366일이어야 합니다.")
        else:
            a, b = (d.isoformat() for d in selected)
            labels = days(a, b)
            automatic = st.checkbox("날짜별·카테고리별 예산을 모두 균등 재배분", key="auto" + suffix)
            initial_day = split_budget(budget, labels) if automatic else {d: doc["daily"].get(d, 0) for d in labels}
            initial_cat = split_budget(budget, CATEGORIES) if automatic else doc["category"]
            editor_key = suffix + str(budget) + a + b + str(automatic)
            st.write("날짜별 예산 (금액 셀을 눌러 수정)")
            day_frame = st.data_editor(pd.DataFrame([{"날짜": d, "예산(원)": v} for d, v in initial_day.items()]),
                                       disabled=["날짜"], hide_index=True, use_container_width=True, key="daily" + editor_key,
                                       column_config={"예산(원)": st.column_config.NumberColumn(min_value=0, max_value=MAX_MONEY, step=1, required=True)})
            st.write("카테고리별 예산")
            cat_frame = st.data_editor(pd.DataFrame([{"카테고리": c, "예산(원)": v} for c, v in initial_cat.items()]),
                                       disabled=["카테고리"], hide_index=True, use_container_width=True, key="cat" + editor_key,
                                       column_config={"예산(원)": st.column_config.NumberColumn(min_value=0, max_value=MAX_MONEY, step=1, required=True)})
            valid_values = all(pd.notna(v) and float(v).is_integer() and v >= 0 for v in list(day_frame["예산(원)"]) + list(cat_frame["예산(원)"]))
            if valid_values:
                daily = dict(zip(day_frame["날짜"], map(int, day_frame["예산(원)"])))
                category = dict(zip(cat_frame["카테고리"], map(int, cat_frame["예산(원)"])))
                st.caption(f"날짜 합계 {won(sum(daily.values()))} / 카테고리 합계 {won(sum(category.values()))} / 전체 {won(budget)}")
                if sum(daily.values()) != budget or sum(category.values()) != budget:
                    st.warning("두 배분 합계가 각각 전체 예산과 일치하도록 조정해 주세요.")
                if st.button("여행 정보 및 예산 저장", type="primary", use_container_width=True):
                    updated = copy.deepcopy(doc)
                    updated.update(destination=dest.strip(), start=a, end=b, people=people, budget=budget, daily=daily, category=category)
                    save(updated)
            else:
                st.error("예산 셀은 비워 두지 말고 0 이상의 정수로 입력해 주세요.")

    with st.expander("🔐 접속·공유 · QR코드"):
        st.warning("비밀코드 소지자는 이 여행을 조회·수정할 수 있습니다. 코드 분실 시 복구할 수 없습니다. 공개 게시하지 마세요.")
        if st.checkbox("비밀코드 보기"):
            st.code(st.session_state.code, language=None)
        st.caption("새로고침하거나 재접속하면 비밀코드를 다시 입력합니다. 데이터는 Supabase에 남습니다. 이름·카드번호 등 민감정보는 입력하지 마세요.")
        app_url = str(st.secrets.get("APP_URL", "")).strip()
        if app_url.startswith("https://") and "?" not in app_url and "#" not in app_url and "@" not in app_url:
            qr = qrcode.make(app_url)
            buffer = io.BytesIO()
            qr.save(buffer, format="PNG")
            st.image(buffer.getvalue(), width=200, caption="앱 접속 QR · 비밀코드는 포함하지 않습니다.")
            st.download_button("접속 QR코드 PNG 내려받기", buffer.getvalue(), "travel_app_qr.png", "image/png", use_container_width=True)
            st.link_button("앱 주소 열기", app_url, use_container_width=True)
        else:
            st.info('QR코드를 만들려면 Secrets의 APP_URL에 "https://앱이름.streamlit.app" 형태의 배포 주소를 설정하세요. 쿼리·비밀코드는 넣지 마세요.')
        if st.button("여행 닫기 (데이터 유지)", use_container_width=True):
            st.session_state.clear()
            st.rerun()


if __name__ == "__main__":
    main()

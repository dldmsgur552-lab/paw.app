"""여행경비 최적화 — 설정/DB/API 키 없이 실행, Python 3.11/3.12 권장.
GitHub에 app.py, requirements.txt 업로드 → Streamlit Cloud에서 app.py 배포.
실행: streamlit run app.py
브라우저 localStorage에 자동 저장. 다른 기기/브라우저와 공유되지 않습니다.
시크릿 모드, 브라우저 데이터 삭제, 앱 주소 변경 시 복원되지 않을 수 있습니다.
JSON 백업을 보관하세요. QR에는 앱 주소만 포함하며 데이터는 포함하지 않습니다.
Streamlit의 브라우저 통신 컴포넌트 파일은 실행 중 자동 생성됩니다.
"""
from __future__ import annotations
import copy
import csv
import html
import io
import json
import math
from pathlib import Path
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import pandas as pd
import qrcode
import streamlit as st
import streamlit.components.v1 as components

CATS = ['교통비', '숙박비', '식비', '관광·체험비', '쇼핑비', '기타']
ICONS = ['🚆', '🏨', '🍜', '🎡', '🛍️', '📦']
PAY = ['카드', '현금', '간편결제', '계좌이체', '기타']
PRI = {1: '1 · 꼭 필요', 2: '2 · 상황에 따라 필요', 3: '3 · 줄이거나 생략 가능'}
LIMIT = 10**12
KST = timezone(timedelta(hours=9))

# Browser-local persistence via Streamlit's public component protocol.
# No third-party scripts, external requests, cookies or shared server-side data files.
COMPONENT_HTML = r'''<!doctype html><html><head><meta charset="utf-8"></head><body><script>
const KEY='travel_budget_local_v2';
function send(type,extra){window.parent.postMessage(Object.assign({isStreamlitMessage:true,type:type},extra||{}),'*');}
let lastToken=null;
window.addEventListener('message',function(event){
 if(event.source!==window.parent || !event.data || event.data.type!=='streamlit:render')return;
 const a=event.data.args;if(!a || a.token===lastToken)return;lastToken=a.token;
 let result={token:a.token,ok:false};
 try{
  if(a.mode==='load'){
   const raw=localStorage.getItem(KEY);
   result.doc=raw===null?null:JSON.parse(raw);
  }else if(a.mode==='save'){
   const raw=JSON.stringify(a.doc);
   localStorage.setItem(KEY,raw);
   if(localStorage.getItem(KEY)!==raw)throw Error('verify');
  }else throw Error('mode');
  result.ok=true;
 }catch(e){result.error=String(e.name||'StorageError');}
 send('streamlit:setComponentValue',{value:result,dataType:'json'});
});
send('streamlit:componentReady',{apiVersion:1});
send('streamlit:setFrameHeight',{height:0});
</script></body></html>'''


def today():
    return datetime.now(KST).date()


def won(v):
    return f'₩{int(v):,}'


def dates(a,b):
    start,end=date.fromisoformat(a),date.fromisoformat(b)
    n=(end-start).days+1
    if not 1<=n<=366: raise ValueError('여행 기간은 1~366일이어야 합니다.')
    return [(start+timedelta(days=i)).isoformat() for i in range(n)]


def split(total,labels):
    q,r=divmod(int(total),len(labels))
    return {k:q+int(i<r) for i,k in enumerate(labels)}


def money(v):
    return isinstance(v,int) and not isinstance(v,bool) and 0<=v<=LIMIT


def validate(d):
    try:
        if not isinstance(d,dict) or d.get('schema')!=2: raise ValueError('백업 형식을 확인해 주세요.')
        if not isinstance(d['destination'],str) or not 1<=len(d['destination'].strip())<=100: raise ValueError('여행지를 입력해 주세요.')
        labels=dates(d['start'],d['end'])
        if not isinstance(d['people'],int) or isinstance(d['people'],bool) or not 1<=d['people']<=10000: raise ValueError('인원은 1~10,000명입니다.')
        if not money(d['budget']) or d['budget']==0: raise ValueError('전체 예산은 1원 이상입니다.')
        for key,keys in [('daily',labels),('category',CATS)]:
            if set(d[key])!=set(keys) or not all(money(v) for v in d[key].values()): raise ValueError('배분 금액은 0 이상의 정수여야 합니다.')
            if sum(d[key].values())!=d['budget']: raise ValueError('날짜별·카테고리별 합계를 각각 전체 예산에 맞춰 주세요.')
        for field in ['expenses','plans']:
            if not isinstance(d[field],list) or len(d[field])>10000: raise ValueError('항목 수가 너무 많거나 형식이 잘못되었습니다.')
            seen=set()
            for e in d[field]:
                if not isinstance(e['id'],str) or e['id'] in seen: raise ValueError('중복 항목입니다.')
                seen.add(e['id'])
                if e['date'] not in labels: raise ValueError('기존 지출·계획이 여행 기간 밖에 있습니다. 항목 날짜를 먼저 수정하세요.')
                if e['category'] not in CATS or e['priority'] not in PRI: raise ValueError('항목 분류를 확인하세요.')
                if not isinstance(e['description'],str) or not 1<=len(e['description'].strip())<=200: raise ValueError('내용은 1~200자로 입력하세요.')
                if not money(e['amount']) or not e['amount']: raise ValueError('항목 금액은 1원 이상입니다.')
                if field=='expenses' and (not money(e['expected']) or e['payment'] not in PAY): raise ValueError('계획액·결제 수단을 확인하세요.')
        pids={p['id'] for p in d['plans']}
        links=[e['plan_id'] for e in d['expenses'] if e.get('plan_id')]
        if len(links)!=len(set(links)) or any(p not in pids for p in links): raise ValueError('계획 결제 연결이 잘못되었습니다.')
    except (KeyError,TypeError,AttributeError,OverflowError):
        raise ValueError('저장 데이터 형식이 올바르지 않습니다. 이 앱에서 받은 JSON 백업을 사용하세요.') from None


def totals(d):
    spent=sum(e['amount'] for e in d['expenses'])
    left=d['budget']-spent
    n=max(0,(date.fromisoformat(d['end'])-max(today(),date.fromisoformat(d['start']))).days+1)
    return spent,left,n


def pending(d):
    paid={e.get('plan_id') for e in d['expenses']}
    return [p for p in d['plans'] if p['id'] not in paid]


def label(e):
    return f"{e['date']} · {e['description']} · {won(e['amount'])}"


def new_doc(destination,start,end,people,budget):
    return dict(schema=2,destination=destination.strip(),start=start,end=end,people=int(people),budget=int(budget),
                daily=split(budget,dates(start,end)),category=split(budget,CATS),expenses=[],plans=[])


def csv_data(d):
    out=io.StringIO(newline=''); w=csv.writer(out)
    w.writerow(['구분','날짜','카테고리','내용','금액(원)','계획액(원)','중요도','결제수단'])
    for kind,items in [('실제 지출',d['expenses']),('미결제 계획',pending(d))]:
        for e in items:
            text=e['description']
            if text.lstrip().startswith(('=','+','-','@','\t','\r')): text="'"+text
            w.writerow([kind,e['date'],e['category'],text,e['amount'],e.get('expected',e['amount']),e['priority'],e.get('payment','')])
    return out.getvalue().encode('utf-8-sig')


def persist(d):
    try: validate(d)
    except ValueError as ex:
        st.error(str(ex)); return
    st.session_state.storage_request={'mode':'save','token':uuid.uuid4().hex,'doc':d}
    st.rerun()


def storage_gate():
    folder=Path(__file__).resolve().parent/'.travel_browser_component'
    folder.mkdir(exist_ok=True)
    path=folder/'index.html'
    if not path.exists() or path.read_text(encoding='utf-8')!=COMPONENT_HTML:
        path.write_text(COMPONENT_HTML,encoding='utf-8')
    bridge=components.declare_component('travel_local_storage_v2',path=str(folder))
    if 'storage_request' not in st.session_state:
        st.session_state.storage_request={'mode':'load','token':uuid.uuid4().hex}
    req=st.session_state.storage_request
    response=bridge(**req,key='travel_storage',default=None)
    if response is None or response.get('token')!=req['token']:
        st.info('이 브라우저의 여행 데이터를 확인하는 중입니다…')
        st.caption('오래 멈추면 쿠키·사이트 저장소 차단을 해제하거나 일반 브라우저에서 다시 열어 주세요.')
        st.stop()
    if st.session_state.get('handled_token')==req['token']: return
    st.session_state.handled_token=req['token']
    if response.get('ok'):
        d=req.get('doc') if req['mode']=='save' else response.get('doc')
        if d is not None:
            try: validate(d)
            except ValueError as ex:
                st.error('저장된 데이터를 읽지 못했습니다: '+str(ex))
                st.session_state.storage_invalid=True
                return
        st.session_state.doc=d
        st.session_state.storage_ok=True
        st.session_state.storage_invalid=False
        if req['mode']=='save':
            st.session_state.revision=st.session_state.get('revision',0)+1
            st.session_state.notice='이 브라우저에 저장 완료 ✓'
    else:
        st.session_state.storage_ok=False
        if req['mode']=='save':
            st.session_state.doc=req['doc']
            st.session_state.revision=st.session_state.get('revision',0)+1
        st.warning('브라우저 저장소를 사용할 수 없습니다. 현재 작업은 임시입니다. 반드시 JSON 백업을 다운로드하세요.')


def restore(key):
    backup=st.file_uploader('JSON 백업',type=['json'],key=key)
    confirm=st.checkbox('현재 여행을 백업 내용으로 교체합니다.',key=key+'confirm')
    if st.button('백업 복원',key=key+'button',disabled=not backup or not confirm,use_container_width=True):
        try:
            raw=backup.getvalue()
            if len(raw)>5_000_000: raise ValueError('백업 파일은 5MB 이하만 가능합니다.')
            d=json.loads(raw);validate(d);persist(d)
        except (ValueError,UnicodeError) as ex: st.error(str(ex))


def create_trip():
    st.caption('가입·접속 키 없이 시작하세요. 데이터는 현재 브라우저에 저장됩니다.')
    with st.form('create'):
        destination=st.text_input('여행지',placeholder='예: 제주도',max_chars=100)
        selected=st.date_input('여행 기간',value=(today(),today()+timedelta(days=2)))
        people=st.number_input('인원수',1,10000,2)
        budget=st.number_input('전체 예산 (₩)',1,LIMIT,1000000,step=10000)
        go=st.form_submit_button('🧳 여행 시작',type='primary',use_container_width=True)
    if go:
        try:
            if len(selected)!=2: raise ValueError('시작일과 종료일을 선택하세요.')
            persist(new_doc(destination,selected[0].isoformat(),selected[1].isoformat(),people,budget))
        except ValueError as ex: st.error(str(ex))
    with st.expander('📂 기존 백업으로 시작'): restore('initial_restore')


def item_editor(d,key,plan=False,existing=None,seed=None):
    b=existing or seed or {};a,z=date.fromisoformat(d['start']),date.fromisoformat(d['end'])
    day=st.date_input('예정일' if plan else '지출일',value=date.fromisoformat(b['date']) if b else min(max(today(),a),z),min_value=a,max_value=z,key=key+'day')
    cat=st.selectbox('카테고리',CATS,index=CATS.index(b.get('category',CATS[0])),format_func=lambda c:ICONS[CATS.index(c)]+' '+c,key=key+'cat')
    desc=st.text_input('내용',value=b.get('description',''),max_chars=200,placeholder='예: 저녁 식사',key=key+'desc')
    amount=int(st.number_input('예정 금액 (₩)' if plan else '실제 금액 (₩)',1,LIMIT,int(b.get('amount',10000)),step=1000,key=key+'amount'))
    pri=st.selectbox('중요도',list(PRI),index=b.get('priority',1)-1,format_func=PRI.get,key=key+'pri')
    e=dict(id=existing['id'] if existing else uuid.uuid4().hex,date=day.isoformat(),category=cat,description=desc.strip(),amount=amount,priority=pri)
    accepted=True
    if not plan:
        exp=int(st.number_input('계획 금액 (₩, 0 = 미설정)',0,LIMIT,int(b.get('expected',seed['amount'] if seed else 0)),step=1000,key=key+'exp'))
        pay=st.selectbox('결제 수단',PAY,index=PAY.index(b.get('payment',PAY[0])),key=key+'pay')
        e.update(expected=exp,payment=pay,plan_id=existing.get('plan_id') if existing else (seed['id'] if seed else None))
        after=totals(d)[1]+(existing['amount'] if existing else 0)-amount
        warn=False
        if exp and amount>exp: st.warning(f'계획보다 {won(amount-exp)} 비쌉니다. 구매·이용을 다시 검토하세요.');warn=True
        if after<0: st.error(f'등록하면 전체 예산을 {won(-after)} 초과합니다.');warn=True
        if warn:
            sig=str((day,cat,desc,amount,exp,pay,pri))
            accepted=st.checkbox('경고를 확인했고 직접 지출을 결정합니다.',key=key+'ack'+sig)
    if st.button('계획 저장' if plan else '지출 확정 · 저장',key=key+'save',type='primary',disabled=not accepted,use_container_width=True):
        updated=copy.deepcopy(d);field='plans' if plan else 'expenses'
        if existing: updated[field]=[e if x['id']==existing['id'] else x for x in updated[field]]
        else: updated[field].append(e)
        persist(updated)


def settings(d):
    rev=str(st.session_state.get('revision',0))
    dest=st.text_input('여행지',value=d['destination'],max_chars=100,key='dest'+rev)
    selected=st.date_input('기간',value=(date.fromisoformat(d['start']),date.fromisoformat(d['end'])),key='range'+rev)
    people=int(st.number_input('인원수',1,10000,d['people'],key='people'+rev))
    budget=int(st.number_input('전체 예산 (₩)',1,LIMIT,d['budget'],step=10000,key='budget'+rev))
    st.caption('1인당 '+won(budget//people)+' · 원 미만 버림')
    if len(selected)!=2: st.info('시작·종료일을 선택하세요.');return
    a,z=(x.isoformat() for x in selected)
    try: labels=dates(a,z)
    except ValueError as ex: st.error(str(ex));return
    auto=st.checkbox('날짜·카테고리 예산 균등 재배분',key='auto'+rev)
    day=split(budget,labels) if auto else {k:d['daily'].get(k,0) for k in labels}
    cat=split(budget,CATS) if auto else d['category']
    suffix=rev+str(budget)+a+z+str(auto)
    config={'예산(원)':st.column_config.NumberColumn(min_value=0,max_value=LIMIT,step=1,required=True)}
    st.caption('금액 셀을 눌러 수정 · 두 합계가 각각 전체 예산과 같아야 저장됩니다.')
    df=st.data_editor(pd.DataFrame([{'날짜':k,'예산(원)':v} for k,v in day.items()]),disabled=['날짜'],hide_index=True,column_config=config,key='dayedit'+suffix,use_container_width=True)
    cf=st.data_editor(pd.DataFrame([{'카테고리':k,'예산(원)':v} for k,v in cat.items()]),disabled=['카테고리'],hide_index=True,column_config=config,key='catedit'+suffix,use_container_width=True)
    if not all(pd.notna(v) and math.isfinite(float(v)) and float(v).is_integer() and 0<=v<=LIMIT for v in list(df['예산(원)'])+list(cf['예산(원)'])):
        st.error('빈칸·음수·소수 없이 입력하세요.');return
    day=dict(zip(df['날짜'],map(int,df['예산(원)'])));cat=dict(zip(cf['카테고리'],map(int,cf['예산(원)'])))
    equal=sum(day.values())==sum(cat.values())==budget
    st.caption(f'날짜 합계 {won(sum(day.values()))} · 카테고리 합계 {won(sum(cat.values()))}')
    if not equal: st.warning('각 합계를 전체 예산에 맞추세요.')
    if st.button('설정 저장',disabled=not equal,type='primary',use_container_width=True):
        updated=copy.deepcopy(d);updated.update(destination=dest.strip(),start=a,end=z,people=people,budget=budget,daily=day,category=cat);persist(updated)


def main():
    st.set_page_config(page_title='여행경비 최적화',page_icon='🧳',layout='centered')
    st.markdown('''<style>
    .stApp{background:#F5F8F4}.block-container{max-width:790px;padding-top:1rem;padding-bottom:2rem}h1{font-size:1.8rem!important}
    button,input{min-height:44px!important}button{border-radius:12px!important}[data-testid="stExpander"]{background:#FFFFFFB0;border-radius:15px}
    .tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:10px 0}.tile{background:#E3F0E8;border:1px solid #C6DECF;border-radius:15px;padding:13px 8px;min-width:0}
    .tile small{font-size:12px;color:#486855}.tile strong{display:block;color:#18452C;font-size:clamp(14px,4vw,24px);overflow-wrap:anywhere;margin-top:5px}
    .bad{background:#FFE8E4;border-color:#EBAEA5}.bad strong{color:#A8281F}
    @media(max-width:600px){.block-container{padding-left:12px;padding-right:12px}.tile{padding:12px 6px}}
    </style>''',unsafe_allow_html=True)
    st.title('🧳 여행경비 최적화')
    storage_gate()
    d=st.session_state.get('doc')
    if st.session_state.get('storage_invalid'):
        st.warning('현재 브라우저 데이터가 손상되었습니다. 새 여행 저장은 기존 데이터를 덮어씁니다.')
    if not st.session_state.get('storage_ok',False): st.warning('영구 저장이 확인되지 않았습니다. JSON 백업을 꼭 보관하세요.')
    if d is None:
        create_trip();return
    if 'notice' in st.session_state: st.toast(st.session_state.pop('notice'))
    spent,left,n=totals(d)
    st.caption(f"{d['destination']} · {d['start']} ~ {d['end']} · {d['people']}명")
    st.markdown(f'''<div class="tiles"><div class="tile"><small>전체 예산</small><strong>{won(d['budget'])}</strong></div><div class="tile"><small>총지출</small><strong>{won(spent)}</strong></div><div class="tile {'bad' if left<0 else ''}"><small>잔여 예산</small><strong>{won(left)}</strong></div></div>''',unsafe_allow_html=True)
    st.progress(min(1.0,spent/d['budget']),text=f"예산 사용률 {spent/d['budget']:.1%}")
    st.caption(f"1인당 예산 {won(d['budget']//d['people'])} · 지출 {won(spent//d['people'])} · 원 미만 버림")
    st.caption(f'남은 {n}일 · 하루 사용 가능 {won(max(0,left)//n)} · 한국시간, 오늘 포함' if n else '여행 종료 · 전체 내역과 CSV를 확인하세요.')
    if left<0: st.error(f'🚨 예산 {won(-left)} 초과! 추가 지출을 검토하세요.')
    pp=pending(d);gap=sum(p['amount'] for p in pp)-left
    if pp and gap>0:
        st.warning(f'미결제 계획까지 지출하면 {won(gap)} 부족합니다.')
        with st.expander('💡 절약 후보 · 중요도 3 → 2'):
            candidates=sorted([p for p in pp if p['priority'] in (3,2)],key=lambda p:(-p['priority'],-p['amount']))
            for p in candidates: st.write(f"중요도 {p['priority']} · {p['description']} · {won(p['amount'])}")
            if not candidates: st.info('줄일 수 있는 계획이 없습니다. 필수 지출과 전체 예산을 검토하세요.')
            st.caption('이미 결제한 지출은 제외됩니다. 취소·구매 여부는 직접 결정하세요.')
    elif left<=d['budget']*.2: st.warning('잔여 예산 20% 이하 · 중요도 3 지출부터 검토하세요.')
    rev=str(st.session_state.get('revision',0))
    with st.expander('➕ 지출 등록',expanded=False):
        choices={'direct':None}|{p['id']:p for p in pp}
        pick=st.selectbox('입력 방식',list(choices),format_func=lambda k:'직접 입력' if k=='direct' else '계획 결제: '+label(choices[k]))
        item_editor(d,'add'+rev+pick,seed=choices[pick])
    with st.expander(f'📝 미결제 계획 · {len(pp)}건'):
        st.caption('계획은 실제 잔여 예산에서 차감하지 않습니다. 지출 등록에서 결제 확정하세요.')
        mode=st.radio('작업',['새 계획','수정·삭제'],horizontal=True)
        if mode=='새 계획': item_editor(d,'plan'+rev,plan=True)
        elif not pp: st.info('미결제 계획이 없습니다.')
        else:
            by={p['id']:p for p in pp};pid=st.selectbox('계획 선택',list(by),format_func=lambda k:label(by[k]))
            item_editor(d,'planedit'+pid+rev,plan=True,existing=by[pid])
            ok=st.checkbox('선택한 계획을 삭제합니다.',key='dp'+pid)
            if st.button('계획 삭제',disabled=not ok,use_container_width=True):
                updated=copy.deepcopy(d);updated['plans']=[p for p in updated['plans'] if p['id']!=pid];persist(updated)
    with st.expander('📊 예산 대비 실제 · 카테고리 / 날짜'):
        rows=[]
        for cat in CATS:
            actual=sum(e['amount'] for e in d['expenses'] if e['category']==cat)
            rows.append({'카테고리':cat,'계획(원)':d['category'][cat],'실제(원)':actual,'잔여(원)':d['category'][cat]-actual,'비율(%)':round(actual/spent*100,1) if spent else 0.0})
        frame=pd.DataFrame(rows).set_index('카테고리')
        st.bar_chart(frame[['계획(원)','실제(원)']],stack=False,color=['#A6CDB7','#177B4B'])
        st.dataframe(frame,use_container_width=True)
        if spent:
            top=max(rows,key=lambda r:r['실제(원)']);st.info(f"가장 큰 지출: {top['카테고리']} · {won(top['실제(원)'])} ({top['비율(%)']}%)")
        daily=[]
        for day in dates(d['start'],d['end']):
            actual=sum(e['amount'] for e in d['expenses'] if e['date']==day)
            daily.append({'날짜':day,'계획(원)':d['daily'][day],'실제(원)':actual,'잔여(원)':d['daily'][day]-actual})
        st.dataframe(pd.DataFrame(daily).set_index('날짜'),use_container_width=True)
        st.caption('잔여가 음수이면 해당 배정 예산을 초과한 상태입니다.')
    with st.expander(f"📒 지출 내역 · {len(d['expenses'])}건 · CSV"):
        st.download_button('CSV 다운로드',csv_data(d),'travel_expenses.csv','text/csv',use_container_width=True)
        st.caption('실제 지출과 미결제 계획을 구분해서 내보냅니다.')
        if not d['expenses']: st.info('등록된 지출이 없습니다.')
        else:
            table=[{'날짜':e['date'],'카테고리':e['category'],'내용':e['description'],'실제(원)':e['amount'],'계획(원)':e['expected'],'중요도':e['priority'],'결제수단':e['payment']} for e in sorted(d['expenses'],key=lambda e:e['date'])]
            st.dataframe(pd.DataFrame(table),hide_index=True,use_container_width=True)
            by={e['id']:e for e in d['expenses']};eid=st.selectbox('지출 선택',list(by),format_func=lambda k:label(by[k]))
            item_editor(d,'edit'+eid+rev,existing=by[eid])
            ok=st.checkbox('지출 삭제 · 연결된 계획은 미결제로 복원됩니다.',key='de'+eid)
            if st.button('지출 삭제',disabled=not ok,use_container_width=True):
                updated=copy.deepcopy(d);updated['expenses']=[e for e in updated['expenses'] if e['id']!=eid];persist(updated)
    with st.expander('⚙️ 여행 정보 · 날짜별 / 카테고리별 예산'): settings(d)
    with st.expander('💾 백업 · 복원 · 새 여행'):
        st.download_button('전체 JSON 백업',json.dumps(d,ensure_ascii=False,indent=2),'travel_backup.json','application/json',use_container_width=True)
        st.caption('사진이나 개인정보 없이 여행 데이터 전체를 백업합니다. 다른 기기에서는 이 JSON을 복원하세요.')
        restore('restore')
        st.divider()
        ok=st.checkbox('새 여행 저장 시 기존 여행을 교체합니다. 먼저 JSON 백업을 보관했습니다.')
        if ok: create_trip()
    with st.expander('📱 QR코드로 앱 접속'):
        address=st.text_input('배포한 앱 주소',placeholder='https://your-app.streamlit.app',max_chars=500)
        if address:
            try:
                u=urlsplit(address.strip())
                valid=u.scheme=='https' and bool(u.hostname) and not any([u.username,u.password,u.query,u.fragment]) and not any(c.isspace() for c in address.strip())
            except ValueError: valid=False
            if valid:
                try:
                    out=io.BytesIO();qrcode.make(address.strip()).save(out,format='PNG')
                    st.image(out.getvalue(),width=190)
                    st.download_button('QR PNG 다운로드',out.getvalue(),'travel_qr.png','image/png',use_container_width=True)
                except (ValueError,qrcode.exceptions.DataOverflowError): st.warning('배포된 앱의 짧은 기본 주소를 입력하세요.')
            else: st.warning('쿼리·로그인 정보가 없는 https 앱 주소를 입력하세요.')
        st.caption('QR은 앱만 엽니다. 여행 데이터는 공유되지 않습니다. 다른 기기에는 JSON 백업을 복원하세요.')
    with st.expander('↻ 다른 탭의 저장 내용 불러오기'):
        st.warning('동시에 여러 탭에서 수정하면 나중에 저장한 내용이 덮어씁니다. 한 탭에서 사용하세요.')
        if st.button('브라우저 저장소 다시 읽기',use_container_width=True):
            st.session_state.storage_request={'mode':'load','token':uuid.uuid4().hex};st.rerun()
    st.caption('이 기기·브라우저 자동 저장 · 사이트 데이터 삭제 시 소실 · JSON 백업 권장')


if __name__=='__main__':
    main()

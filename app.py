import streamlit as st
import requests
import time
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

# Try imports with error handling
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError as e:
    st.error(f"Missing Dependencies: {e}")
    st.stop()

# ─── Configuration ────────────────────────────────────────────────────────────
BASE = "https://volunteer.bloomerang.co"
ORG_ID = 5269
EVENT_ID = 51764
LOCAL_TZ = ZoneInfo("America/New_York")

st.set_page_config(page_title="Refuge Live Board", page_icon="🐾", layout="wide")

# ─── Custom CSS ───
st.markdown("""
<style>
    .shift-card {
        background-color: #ffffff;
        padding: 1.4rem;
        border-radius: 14px;
        margin-bottom: 1.2rem;
        border-left: 10px solid #cbd5e0;
        box-shadow: 0 4px 12px rgba(0,0,0,0.08);
        color: #1a202c;
    }
    [data-theme="dark"] .shift-card { background-color: #1e2533; color: #f7fafc; border-left-color: #4a5568; }
    
    .shift-time { font-size: 0.95rem; font-weight: 700; color: #718096; margin-bottom: 0.5rem; }
    .shift-name { font-size: 1.5rem; font-weight: 900; margin-bottom: 0.2rem; line-height: 1.1; color: #1a202c; }
    [data-theme="dark"] .shift-name { color: #ffffff; }
    .shift-role { font-size: 0.85rem; text-transform: uppercase; color: #a0aec0; font-weight: 800; margin-bottom: 1rem; letter-spacing: 0.08em; }
    
    .status-badge { padding: 0.5rem 0.9rem; border-radius: 20px; font-weight: 900; font-size: 0.75rem; text-transform: uppercase; display: inline-block; }
    
    .status-checked-in { border-left-color: #2f855a !important; background-color: rgba(47, 133, 90, 0.1); }
    .status-checked-in .status-badge { background-color: #2f855a; color: white; }
    
    .status-completed { border-left-color: #6b46c1 !important; background-color: rgba(107, 70, 193, 0.1); }
    .status-completed .status-badge { background-color: #6b46c1; color: white; }
    
    .status-alert-red { border-left-color: #c53030 !important; background-color: rgba(197, 48, 48, 0.1); }
    .status-alert-red .status-badge { background-color: #c53030; color: white; }
    
    .status-upcoming { border-left-color: #2b6cb0 !important; background-color: rgba(43, 108, 176, 0.1); }
    .status-upcoming .status-badge { background-color: #2b6cb0; color: white; }
    
    .status-pending { border-left-color: #718096 !important; background-color: #f7fafc; }
    .status-pending .status-badge { background-color: #4a5568; color: white; }
    
    .punch-box {
        margin-top: 12px; padding: 8px 12px; background: rgba(0,0,0,0.05); border-radius: 8px;
        font-family: 'JetBrains Mono', 'Courier New', monospace; font-size: 0.9rem; font-weight: 700; color: #2d3748;
    }
    [data-theme="dark"] .punch-box { background: rgba(255,255,255,0.08); color: #e2e8f0; }
</style>
""", unsafe_allow_html=True)

def authenticate_headless(email, password):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    if os.path.exists("/usr/bin/chromium"): options.binary_location = "/usr/bin/chromium"
    
    driver = None
    try:
        service = Service("/usr/bin/chromedriver") if os.path.exists("/usr/bin/chromedriver") else Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 15)
        
        driver.get(f"{BASE}/volunteer/#/login")
        time.sleep(3)
        
        wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@type='email' or @type='text']"))).send_keys(email)
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(translate(., 'NEXT', 'next'), 'next')]"))).click()
        
        time.sleep(2)
        wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@type='password']"))).send_keys(password)
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(translate(., 'LOG IN', 'log in'), 'log in')]"))).click()
        
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'My Shifts') or contains(text(), 'Welcome')]")))
            time.sleep(2) 
        except:
            raise Exception("Timeout waiting for dashboard to load.")
            
        sess = requests.Session()
        for c in driver.get_cookies(): 
            sess.cookies.set(c['name'], c['value'])
            
        token = driver.execute_script("""
            for (let i = 0; i < localStorage.length; i++) {
                let key = localStorage.key(i);
                if (key.includes('idToken') || key.includes('accessToken')) {
                    return localStorage.getItem(key);
                }
            }
            return null;
        """)
        
        return {"sess": sess, "token": token}
    except Exception as e:
        st.error(f"Login failed: {e}")
        return None
    finally:
        if driver: driver.quit()

def safe_get_json(auth_dict, url, params=None):
    sess = auth_dict['sess']
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': f'{BASE}/volunteer/',
    }
    if auth_dict.get('token'):
        headers['Authorization'] = f"Bearer {auth_dict['token']}"
        
    try:
        r = sess.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 401: return "AUTH_EXPIRED"
        if r.status_code != 200: return f"ERR_{r.status_code}: {r.text[:150]}"
        return r.json()
    except Exception as e:
        return f"ERR_REQ: {str(e)}"

@st.cache_data(ttl=60)
def get_dashboard_data(_auth_dict, target_date_obj):
    if not _auth_dict: return None, None
    
    # 1. Fetch data - Added extra params to grab past shifts and up to 500 records
    shifts_params = {"includeShiftRoles": "true", "includeShiftUsers": "true", "take": 500, "includePast": "true"}
    s_raw = safe_get_json(_auth_dict, f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts", shifts_params)
    
    if isinstance(s_raw, str): return s_raw, None 
    if not s_raw: return "ERR_EMPTY", None
    
    shift_defs = {s['id']: s for s in s_raw}
    
    enrollments = safe_get_json(_auth_dict, f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/enrollments", {"take": 500})
    if isinstance(enrollments, str): enrollments = []
    
    attendance = safe_get_json(_auth_dict, f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/attendance", {"take": 500})
    if isinstance(attendance, str): attendance = []
    
    raw_people = []
    uids = set()
    seen_keys = set()
    
    # 2. Extract people (Safely falls back if the shift is missing from the API)
    def process_person(item):
        uid = item.get('userId') or item.get('id')
        sid = item.get('eventShiftId') or item.get('shiftId')
        if not uid or not sid: return
        
        s_def = shift_defs.get(sid)
        
        # If the API drops the shift because it's in the past, reconstruct it from the attendance record
        if s_def:
            s_start = datetime.fromisoformat(s_def['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            s_end = datetime.fromisoformat(s_def['endDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            
            rid = item.get('eventRoleId')
            r_name = "Volunteer"
            for r in s_def.get('roles', []):
                if r.get('id') == rid:
                    r_name = r.get("eventRoleTexts", [{}])[0].get("eventRoleName", "Volunteer")
                    break
        else:
            start_raw = item.get('shiftStartDate') or item.get('startDate') or item.get('startTimestamp')
            end_raw = item.get('shiftEndDate') or item.get('endDate') or item.get('endTimestamp')
            
            if not start_raw: return # Can't place on board without any time data
            
            s_start = datetime.fromisoformat(start_raw.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            s_end = datetime.fromisoformat(end_raw.replace('Z', '+00:00')).astimezone(LOCAL_TZ) if end_raw else s_start + timedelta(hours=1)
            r_name = item.get('eventRoleName') or item.get('roleName') or "Volunteer"

        if s_start.date() != target_date_obj: return
        
        key = f"{sid}-{uid}"
        if key in seen_keys: return
        
        raw_people.append({
            'uid': uid, 'sid': sid, 
            'fname': item.get('firstName', '').strip(), 
            'lname': item.get('lastName', '').strip(), 
            'role': r_name,
            'start': s_start, 
            'end': s_end
        })
        uids.add(uid)
        seen_keys.add(key)

    for e in enrollments: process_person(e)
    for a in attendance: process_person(a)
    for sid, sdef in shift_defs.items():
        for role in sdef.get('roles', []):
            for user in role.get('users', []):
                process_person({'userId': user.get('id'), 'eventShiftId': sid, 'firstName': user.get('firstName'), 'lastName': user.get('lastName'), 'eventRoleId': role.get('id')})

    # 3. Enrichment
    punch_map = {}
    profile_map = {}

    def fetch_meta(uid):
        p_raw = safe_get_json(_auth_dict, f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime")
        prof_raw = safe_get_json(_auth_dict, f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}")
        
        plist = p_raw if isinstance(p_raw, list) else []
        pdict = prof_raw if isinstance(prof_raw, dict) else {}
        return uid, plist, pdict

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch_meta, uid) for uid in uids]
        for f in as_completed(futures):
            uid, p_list, prof = f.result()
            punch_map[uid] = p_list
            profile_map[uid] = prof

    # 4. Final Cleanup
    final_roster = []
    for p in raw_people:
        prof = profile_map.get(p['uid'], {})
        fname = p['fname'] if p['fname'] else prof.get('firstName', '')
        lname = p['lname'] if p['lname'] else prof.get('lastName', '')
        
        if not fname or fname.lower() in ["none", "volunteer"]: 
            fname = "Unknown"
            lname = f"(ID: {p['uid']})"
            
        p['fname'], p['lname'] = fname, lname
        final_roster.append(p)
        
    return final_roster, punch_map

# ─── App UI ───
if 'auth_data' not in st.session_state: 
    st.session_state.auth_data = None

with st.sidebar:
    st.title("🐾 Staff Access")
    if st.session_state.auth_data is None:
        with st.form("auth"):
            u = st.text_input("Email")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Log In"):
                st.session_state.auth_data = authenticate_headless(u, p)
                if st.session_state.auth_data: st.rerun()
    else:
        st.success("Connected")
        if st.button("Refresh Board"): st.cache_data.clear(); st.rerun()
        if st.button("Logout"): st.session_state.auth_data = None; st.rerun()

if st.session_state.auth_data:
    now = datetime.now(LOCAL_TZ)
    t_date = now.date()
    
    st.title(f"Refuge Roster — {t_date.strftime('%A, %b %d')}")
    
    with st.spinner("Syncing Bloomerang..."):
        data = get_dashboard_data(st.session_state.auth_data, t_date)
        
        if isinstance(data[0], str):
            if data[0] == "AUTH_EXPIRED":
                st.session_state.auth_data = None
                st.error("Session Expired. Please log in again.")
                st.stop()
            else:
                st.error(f"API Error: {data[0]}")
                st.stop()
                
        roster, punches = data
    
    if roster:
        cards = []
        for v in roster:
            fullName = f"{v['fname']} {v['lname']}".strip()
            user_p = punches.get(v['uid'], [])
            my_punch = next((p for p in user_p if p.get('eventShiftId') == v['sid']), None)
            
            if not my_punch:
                for p in user_p:
                    if p.get('startTimestamp'):
                        dt = datetime.fromisoformat(p['startTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                        if dt.date() == t_date: my_punch = p; break

            cin = datetime.fromisoformat(my_punch['startTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ) if my_punch and my_punch.get('startTimestamp') else None
            cout = datetime.fromisoformat(my_punch['endTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ) if my_punch and my_punch.get('endTimestamp') else None
            p_str = f"In: {cin.strftime('%I:%M %p') if cin else '--'} → Out: {cout.strftime('%I:%M %p') if cout else '--'}"
            
            status, css = "Scheduled", "status-pending"
            if cin and cout: status, css = "Completed", "status-completed"
            elif cin: status, css = ("Late Out", "status-alert-red") if now > v['end'] + timedelta(minutes=15) else ("On Shift", "status-checked-in")
            else:
                if now > v['start'] + timedelta(minutes=15): status, css = "No Show / Late", "status-alert-red"
                elif now >= v['start'] - timedelta(minutes=60): status, css = "Starting Soon", "status-upcoming"

            cards.append({
                "time": v['start'],
                "html": f"""
                <div class="shift-card {css}">
                    <div class="shift-time">{v['start'].strftime("%I:%M %p")} - {v['end'].strftime("%I:%M %p")}</div>
                    <div class="shift-name">{fullName}</div>
                    <div class="shift-role">{v['role']}</div>
                    <div class="punch-box">🕒 {p_str}</div>
                    <div style="margin-top:12px;"><span class="status-badge">{status}</span></div>
                </div>
                """
            })
        
        cards.sort(key=lambda x: x['time'])
        cols = st.columns(4)
        for i, card in enumerate(cards):
            with cols[i % 4]: st.markdown(card['html'], unsafe_allow_html=True)
    else:
        st.info(f"No volunteers scheduled for {t_date.strftime('%m/%d')}.")
    
    time.sleep(60); st.rerun()
else:
    st.info("Staff Login Required.")

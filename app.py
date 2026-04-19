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
        padding: 1.2rem;
        border-radius: 12px;
        margin-bottom: 1rem;
        border-left: 8px solid #dee2e6;
        box-shadow: 0 4px 6px rgba(0,0,0,0.07);
        color: #1a202c;
    }
    [data-theme="dark"] .shift-card { background-color: #1a202c; color: #f7fafc; border-left-color: #4a5568; }
    
    .shift-time { font-size: 0.9rem; font-weight: 700; color: #718096; margin-bottom: 0.4rem; }
    .shift-name { font-size: 1.3rem; font-weight: 800; margin-bottom: 0.2rem; line-height: 1.2; color: #2d3748; }
    [data-theme="dark"] .shift-name { color: #edf2f7; }
    .shift-role { font-size: 0.8rem; text-transform: uppercase; color: #a0aec0; font-weight: 700; margin-bottom: 0.8rem; letter-spacing: 0.05em; }
    
    .status-badge { padding: 0.4rem 0.8rem; border-radius: 20px; font-weight: 800; font-size: 0.7rem; text-transform: uppercase; display: inline-block; }
    
    .status-checked-in { border-left-color: #38a169 !important; background-color: rgba(56, 161, 105, 0.1); }
    .status-checked-in .status-badge { background-color: #38a169; color: white; }
    
    .status-completed { border-left-color: #805ad5 !important; background-color: rgba(128, 90, 213, 0.1); }
    .status-completed .status-badge { background-color: #805ad5; color: white; }
    
    .status-alert-red { border-left-color: #e53e3e !important; background-color: rgba(229, 62, 62, 0.1); }
    .status-alert-red .status-badge { background-color: #e53e3e; color: white; }
    
    .status-upcoming { border-left-color: #3182ce !important; background-color: rgba(49, 130, 206, 0.1); }
    .status-upcoming .status-badge { background-color: #3182ce; color: white; }
    
    .status-pending { border-left-color: #a0aec0 !important; background-color: #f7fafc; }
    .status-pending .status-badge { background-color: #718096; color: white; }
    
    .punch-box {
        margin-top: 10px; padding: 6px 10px; background: rgba(0,0,0,0.04); border-radius: 6px;
        font-family: 'JetBrains Mono', 'Courier New', monospace; font-size: 0.85rem; font-weight: 600; color: #4a5568;
    }
    [data-theme="dark"] .punch-box { background: rgba(255,255,255,0.05); color: #a0aec0; }
</style>
""", unsafe_allow_html=True)

def authenticate_headless(email, password):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    if os.path.exists("/usr/bin/chromium"):
        options.binary_location = "/usr/bin/chromium"
    
    sess = requests.Session()
    driver = None
    try:
        if os.path.exists("/usr/bin/chromedriver"):
            service = Service("/usr/bin/chromedriver")
        else:
            service = Service(ChromeDriverManager().install())
            
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 15) 
        
        driver.get(f"{BASE}/volunteer/#/login")
        time.sleep(4) 
        
        email_field = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@type='email' or @type='text']")))
        email_field.click()
        email_field.send_keys(email)
        
        next_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(translate(., 'NEXT', 'next'), 'next')] | //button[@type='submit']")))
        next_btn.click()
        
        time.sleep(3) 
        pass_field = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@type='password']")))
        pass_field.click()
        pass_field.send_keys(password)
        
        login_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(translate(., 'LOG IN', 'log in'), 'log in') or contains(., 'Sign In') or @type='submit']")))
        login_btn.click()
        
        time.sleep(8) 
        
        cookies = driver.get_cookies()
        if not cookies or len(cookies) < 2:
            raise Exception("No cookies returned.")
            
        for cookie in cookies:
            sess.cookies.set(cookie['name'], cookie['value'])
            
        return sess
    except Exception as e:
        st.error(f"Login failed: {str(e)}")
        return None
    finally:
        if driver:
            try: driver.quit()
            except: pass

@st.cache_data(ttl=60)
def get_dashboard_data(_sess, target_date_obj):
    if _sess is None: return None, None
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': f'{BASE}/volunteer/',
        }
        
        # 1. Fetch Structural Data
        r_shifts = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts", params={"includeShiftRoles": "true", "includeShiftUsers": "true"}, headers=headers)
        if r_shifts.status_code != 200: return None, None
        shift_defs = {s['id']: s for s in r_shifts.json()}
        
        # 2. Fetch Assignments
        r_enroll = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/enrollments", headers=headers)
        enrollments = r_enroll.json() if r_enroll.status_code == 200 else []
        
        # 3. Fetch Attendance
        r_att = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/attendance", headers=headers)
        attendance = r_att.json() if r_att.status_code == 200 else []
        
        master_assignments = []
        uids = set()
        seen_keys = set()

        def add_entry(sid, uid, fn, ln, rid):
            if not sid or not uid or uid == 0: return # Filter invalid IDs
            sdef = shift_defs.get(sid)
            if not sdef: return
            
            s_local = datetime.fromisoformat(sdef['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            if s_local.date() != target_date_obj: return
            
            key = f"{sid}-{uid}"
            if key in seen_keys: return

            # Get Role Name
            role_name = "Volunteer"
            for r in sdef.get('roles', []):
                if r.get('id') == rid:
                    role_name = r.get("eventRoleTexts", [{}])[0].get("eventRoleName", "Volunteer")
                    break

            master_assignments.append({
                'sid': sid, 'uid': uid, 
                'fname': fn if (fn and fn != "Volunteer" and fn != "None") else "", 
                'lname': ln if (ln and ln != "None") else "",
                'role': role_name,
                'start': s_local,
                'end': datetime.fromisoformat(sdef['endDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            })
            uids.add(uid)
            seen_keys.add(key)

        # Merge data sources
        for e in enrollments: add_entry(e.get('eventShiftId'), e.get('userId'), e.get('firstName'), e.get('lastName'), e.get('eventRoleId'))
        for a in attendance: add_entry(a.get('eventShiftId'), a.get('userId'), a.get('firstName'), a.get('lastName'), a.get('eventRoleId'))
        for sid, sdef in shift_defs.items():
            for role in sdef.get('roles', []):
                for user in role.get('users', []):
                    add_entry(sid, user.get('id'), user.get('firstName'), user.get('lastName'), role['id'])

        # 4. Cleanup Empty/Ghost Cards
        punch_map = {}
        profile_map = {}

        def fetch_meta(uid):
            r_s = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime", headers=headers)
            r_p = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}", headers=headers)
            return uid, (r_s.json() if r_s.status_code == 200 else []), (r_p.json() if r_p.status_code == 200 else {})

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(fetch_meta, uid) for uid in uids if uid]
            for f in as_completed(futures):
                uid, p_list, p_dict = f.result()
                punch_map[uid] = p_list
                profile_map[uid] = p_dict

        # Final Name and Empty Check
        final_list = []
        for a in master_assignments:
            prof = profile_map.get(a['uid'], {})
            # If we STILL have no name, try the profile endpoint's data
            if not a['fname'] or a['fname'] == "Volunteer":
                a['fname'] = prof.get('firstName', '').strip()
                a['lname'] = prof.get('lastName', '').strip()
            
            # Final sanity check: if there is no name at all, it's an unfilled shift
            if not a['fname'] or a['fname'].lower() == "none":
                continue
            
            final_list.append(a)

        return final_list, punch_map
    except Exception as e:
        st.error(f"Sync failed: {e}")
        return None, None

# ─── App UI ───
if 'sess' not in st.session_state:
    st.session_state.sess = None

with st.sidebar:
    st.title("🐾 Staff Portal")
    if st.session_state.sess is None:
        with st.form("login"):
            em = st.text_input("Email")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Log In"):
                st.session_state.sess = authenticate_headless(em, pw)
                if st.session_state.sess: st.rerun()
    else:
        st.success("Connected")
        if st.button("🔄 Refresh Board"): 
            st.cache_data.clear()
            st.rerun()
        if st.button("🚪 Logout"): 
            st.session_state.sess = None
            st.rerun()

if st.session_state.sess:
    now = datetime.now(LOCAL_TZ)
    c1, c2 = st.columns([3, 1])
    with c2:
        t_date = st.date_input("Select Date", value=now.date())
    with c1:
        st.title(f"Roster — {t_date.strftime('%A, %b %d')}")
    
    with st.spinner("Updating board..."):
        assigns, punches = get_dashboard_data(st.session_state.sess, t_date)
    
    if assigns:
        cards = []
        for a in assigns:
            fname = f"{a['fname']} {a['lname']}".strip()
            user_punches = punches.get(a['uid'], [])
            punch = next((p for p in user_punches if p.get('eventShiftId') == a['sid']), None)
            
            if not punch:
                for p in user_punches:
                    if p.get('startTimestamp'):
                        p_start = datetime.fromisoformat(p['startTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                        if p_start.date() == t_date:
                            punch = p; break

            cin = datetime.fromisoformat(punch['startTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ) if punch and punch.get('startTimestamp') else None
            cout = datetime.fromisoformat(punch['endTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ) if punch and punch.get('endTimestamp') else None
            p_str = f"In: {cin.strftime('%I:%M%p') if cin else '--'} → Out: {cout.strftime('%I:%M%p') if cout else '--'}"

            status, css = "Scheduled", "status-pending"
            if cin and cout:
                status, css = "Completed", "status-completed"
            elif cin:
                status, css = ("Missing Out", "status-alert-red") if now > a['end'] + timedelta(minutes=20) else ("On Shift", "status-checked-in")
            else:
                if now > a['start'] + timedelta(minutes=20): status, css = "Late/No Show", "status-alert-red"
                elif now >= a['start'] - timedelta(minutes=60): status, css = "Upcoming", "status-upcoming"

            cards.append({
                "time": a['start'],
                "html": f"""
                <div class="shift-card {css}">
                    <div class="shift-time">{a['start'].strftime("%I:%M %p")} - {a['end'].strftime("%I:%M %p")}</div>
                    <div class="shift-name">{fname}</div>
                    <div class="shift-role">{a['role']}</div>
                    <div class="punch-box">🕒 {p_str}</div>
                    <div style="margin-top:12px;"><span class="status-badge">{status}</span></div>
                </div>
                """
            })
        
        cards.sort(key=lambda x: x['time'])
        cols = st.columns(4)
        for i, c in enumerate(cards):
            with cols[i % 4]: st.markdown(c['html'], unsafe_allow_html=True)
    else:
        st.info(f"No volunteers signed up for {t_date.strftime('%m/%d')}.")
    
    time.sleep(60); st.rerun()
else:
    st.warning("Please log in using the sidebar.")

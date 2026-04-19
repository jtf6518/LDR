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
        background-color: #f8f9fa;
        padding: 1.2rem;
        border-radius: 10px;
        margin-bottom: 1rem;
        border-left: 8px solid #dee2e6;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        color: #1a1a1a;
    }
    [data-theme="dark"] .shift-card { background-color: #1e2130; color: #ffffff; }
    .shift-time { font-size: 1rem; font-weight: 700; color: #666; margin-bottom: 0.3rem; }
    .shift-name { font-size: 1.4rem; font-weight: 800; margin-bottom: 0.1rem; line-height: 1.2; }
    .shift-role { font-size: 0.85rem; text-transform: uppercase; color: #888; font-weight: 600; margin-bottom: 0.8rem; }
    .status-badge { padding: 0.4rem 0.8rem; border-radius: 20px; font-weight: 700; font-size: 0.75rem; text-transform: uppercase; }
    
    .status-checked-in { border-left-color: #28a745 !important; background-color: rgba(40, 167, 69, 0.08); }
    .status-checked-in .status-badge { background-color: #28a745; color: white; }
    
    .status-completed { border-left-color: #8b5cf6 !important; background-color: rgba(139, 92, 246, 0.08); }
    .status-completed .status-badge { background-color: #8b5cf6; color: white; }
    
    .status-alert-red { border-left-color: #dc3545 !important; background-color: rgba(220, 53, 69, 0.08); }
    .status-alert-red .status-badge { background-color: #dc3545; color: white; }
    
    .status-late { border-left-color: #fd7e14 !important; background-color: rgba(253, 126, 20, 0.08); }
    .status-late .status-badge { background-color: #fd7e14; color: white; }
    
    .status-pending { border-left-color: #6c757d !important; background-color: #f8f9fa; }
    .status-pending .status-badge { background-color: #6c757d; color: white; }
    
    .status-upcoming { border-left-color: #007bff !important; background-color: rgba(0, 123, 255, 0.08); }
    .status-upcoming .status-badge { background-color: #007bff; color: white; }
    
    .punch-time {
        font-size: 0.85rem; color: #718096; margin-bottom: 1rem; font-weight: 600; 
        background: rgba(0,0,0,0.05); display: inline-block; padding: 4px 10px; border-radius: 6px;
        font-family: 'JetBrains Mono', monospace;
    }
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
        
        # 1. Fetch Shifts (Structure)
        shifts_url = f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts"
        r_shifts = _sess.get(shifts_url, params={"includeShiftRoles": "true", "includeShiftUsers": "true"}, headers=headers)
        if r_shifts.status_code != 200: return None, None
        
        all_shifts_raw = r_shifts.json()
        shift_defs = {s['id']: s for s in all_shifts_raw}
        
        # 2. Fetch Enrollments (Assignments)
        enroll_url = f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/enrollments"
        r_enroll = _sess.get(enroll_url, headers=headers)
        enrollments = r_enroll.json() if r_enroll.status_code == 200 else []
        
        # Merge sources to find every volunteer assigned for that specific day
        master_assignments = []
        unique_keys = set()
        user_ids = set()

        def register_assignment(sid, uid, fname, lname, rid):
            if not sid or not uid: return
            sdef = shift_defs.get(sid)
            if not sdef: return
            
            # Use Local Time for the day comparison
            start_local = datetime.fromisoformat(sdef['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            if start_local.date() != target_date_obj: return
            
            key = f"{sid}-{uid}"
            if key in unique_keys: return
            
            # Find Role Name
            rname = "Volunteer"
            for r in sdef.get('roles', []):
                if r.get('id') == rid:
                    rname = r.get("eventRoleTexts", [{}])[0].get("eventRoleName", "Volunteer")
                    break

            master_assignments.append({
                'sid': sid, 'uid': uid, 
                'fname': fname if fname else "Volunteer", 
                'lname': lname if lname else "",
                'role': rname,
                'start': start_local,
                'end': datetime.fromisoformat(sdef['endDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            })
            unique_keys.add(key)
            user_ids.add(uid)

        # Source A: Direct enrollments list
        for e in enrollments:
            register_assignment(e.get('eventShiftId'), e.get('userId'), e.get('firstName'), e.get('lastName'), e.get('eventRoleId'))

        # Source B: Nested users in shift objects
        for sid, sdef in shift_defs.items():
            for role in sdef.get('roles', []):
                for user in role.get('users', []):
                    register_assignment(sid, user['id'], user.get('firstName'), user.get('lastName'), role['id'])

        # 3. Batch Fetch Punches and Profile Data
        punch_map = {}
        profile_map = {}

        def fetch_user_context(uid):
            # Service Time (Punches)
            s_url = f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime"
            r_s = _sess.get(s_url, headers=headers)
            punches = r_s.json() if r_s.status_code == 200 else []
            
            # Profile (Real names fallback)
            p_url = f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}"
            r_p = _sess.get(p_url, headers=headers)
            prof = r_p.json() if r_p.status_code == 200 else {}
            
            return uid, punches, prof

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(fetch_user_context, uid) for uid in user_ids]
            for f in as_completed(futures):
                uid, p_data, prof = f.result()
                punch_map[uid] = p_data
                profile_map[uid] = prof

        # 4. Data Cleanup: Replace "Volunteer" placeholders with real names from profiles
        for a in master_assignments:
            p = profile_map.get(a['uid'], {})
            if a['fname'] == "Volunteer" and p.get('firstName'):
                a['fname'] = p['firstName']
                a['lname'] = p.get('lastName', '')

        return master_assignments, punch_map
    except Exception as e:
        st.error(f"Sync failed: {e}")
        return None, None

# ─── App UI ───
if 'sess' not in st.session_state:
    st.session_state.sess = None

with st.sidebar:
    st.title("🐾 Staff Access")
    if st.session_state.sess is None:
        with st.form("auth_form"):
            email = st.text_input("Bloomerang Email")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Log In"):
                with st.spinner("Authenticating..."):
                    st.session_state.sess = authenticate_headless(email, pw)
                    if st.session_state.sess: st.rerun()
    else:
        st.success("Session Active")
        if st.button("Refresh Board"): 
            st.cache_data.clear()
            st.rerun()
        if st.button("Log Out"): 
            st.session_state.sess = None
            st.rerun()

if st.session_state.sess:
    now = datetime.now(LOCAL_TZ)
    col1, col2 = st.columns([3, 1])
    with col2:
        target_date = st.date_input("📅 Select Date", value=now.date())
    with col1:
        st.title(f"Refuge Roster — {target_date.strftime('%A, %b %d')}")
    
    with st.spinner("Finding volunteers..."):
        assignments, punch_data = get_dashboard_data(st.session_state.sess, target_date)
    
    if assignments:
        cards = []
        for a in assignments:
            full_name = f"{a['fname']} {a['lname']}".strip()
            start, end = a['start'], a['end']
            
            # Find the punch that overlaps with this shift
            user_punches = punch_data.get(a['uid'], [])
            punch = next((p for p in user_punches if p.get('eventShiftId') == a['sid']), None)
            
            # If no ID link, try date overlap
            if not punch:
                for p in user_punches:
                    if p.get('startTimestamp'):
                        p_start = datetime.fromisoformat(p['startTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                        if p_start.date() == target_date:
                            punch = p
                            break

            cin_dt = datetime.fromisoformat(punch['startTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ) if punch and punch.get('startTimestamp') else None
            cout_dt = datetime.fromisoformat(punch['endTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ) if punch and punch.get('endTimestamp') else None
            
            c_in_str = cin_dt.strftime('%I:%M %p') if cin_dt else "--"
            c_out_str = cout_dt.strftime('%I:%M %p') if cout_dt else "--"
            time_display = f"In: {c_in_str} → Out: {c_out_str}"

            status, css = "Pending", "status-pending"
            if cin_dt and cout_dt:
                status, css = "Completed", "status-completed"
            elif cin_dt:
                if now > end + timedelta(minutes=15):
                    status, css = "Missing Out", "status-alert-red"
                else:
                    status, css = "Checked In", "status-checked-in"
            else:
                if now > start + timedelta(minutes=15):
                    status, css = "Missing In", "status-alert-red"
                elif now >= start - timedelta(minutes=60):
                    status, css = "Due Soon", "status-upcoming"

            cards.append({
                "time": start,
                "html": f"""
                <div class="shift-card {css}">
                    <div class="shift-time">{start.strftime("%I:%M %p")} - {end.strftime("%I:%M %p")}</div>
                    <div class="shift-name">{full_name}</div>
                    <div class="shift-role">{a['role']}</div>
                    <div class="punch-time">🕒 {time_display}</div>
                    <br/>
                    <span class="status-badge">{status}</span>
                </div>
                """
            })
        
        cards.sort(key=lambda x: x['time'])
        cols = st.columns(4)
        for i, c in enumerate(cards):
            with cols[i % 4]: st.markdown(c['html'], unsafe_allow_html=True)
    else:
        st.info(f"No volunteer data found for {target_date.strftime('%m/%d')}. Please refresh or try another date.")
                
    time.sleep(60)
    st.rerun()
else:
    st.info("Please log in to view the board.")

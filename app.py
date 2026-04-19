import streamlit as st
import requests
import time
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

# Try imports with error handling to help debug in the Streamlit UI
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
except ImportError as e:
    st.error(f"Missing Dependencies: {e}. Please ensure requirements.txt exists in your GitHub root.")
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
    .shift-name { font-size: 1.4rem; font-weight: 800; margin-bottom: 0.1rem; }
    .shift-role { font-size: 0.85rem; text-transform: uppercase; color: #888; font-weight: 600; margin-bottom: 1rem; }
    .status-badge { padding: 0.4rem 0.8rem; border-radius: 20px; font-weight: 700; font-size: 0.75rem; text-transform: uppercase; }
    
    .status-checked-in { border-left-color: #28a745 !important; background-color: rgba(40, 167, 69, 0.08); }
    .status-checked-in .status-badge { background-color: #28a745; color: white; }
    .status-checked-out { border-left-color: #fd7e14 !important; background-color: rgba(253, 126, 20, 0.08); }
    .status-checked-out .status-badge { background-color: #fd7e14; color: white; }
    .status-alert-red { border-left-color: #dc3545 !important; background-color: rgba(220, 53, 69, 0.08); }
    .status-alert-red .status-badge { background-color: #dc3545; color: white; }
    .status-pending { border-left-color: #6c757d !important; background-color: #f8f9fa; }
    .status-pending .status-badge { background-color: #6c757d; color: white; }
    .status-upcoming { border-left-color: #007bff !important; background-color: rgba(0, 123, 255, 0.08); }
    .status-upcoming .status-badge { background-color: #007bff; color: white; }
</style>
""", unsafe_allow_html=True)

def authenticate_headless(email, password):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
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
        driver.get(f"{BASE}/volunteer/#/login")
        
        wait = WebDriverWait(driver, 20)
        time.sleep(5) 

        # Iframe Handling
        if len(driver.find_elements(By.TAG_NAME, "iframe")) > 0:
            driver.switch_to.frame(0)
            
        # STEP 1: Email
        # Using multi-selector approach for robustness
        email_field = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='username'], #username, input[type='email']")))
        email_field.clear()
        email_field.send_keys(email)
        
        next_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'], .submit-button, #submit-button, button.btn-primary")))
        next_btn.click()
        
        # STEP 2: Password
        # We wait for the password field to appear. 
        # Sometimes Cognito transitions within the same frame, sometimes it reloads.
        time.sleep(3)
        try:
            # Re-check frame if element is not found immediately
            pass_field = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='password'], #password, input[type='password']")))
        except TimeoutException:
            # Try switching back to main and checking frames again
            driver.switch_to.default_content()
            if len(driver.find_elements(By.TAG_NAME, "iframe")) > 0:
                driver.switch_to.frame(0)
            pass_field = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='password'], #password, input[type='password']")))

        pass_field.clear()
        pass_field.send_keys(password)
        
        login_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'], .submit-button, #submit-button, button.btn-primary")))
        login_btn.click()
        
        # STEP 3: Capturing Session
        time.sleep(10)
        
        for _ in range(5):
            cookies = driver.get_cookies()
            if cookies and any('cognito' in c['name'].lower() or 'session' in c['name'].lower() for c in cookies):
                for cookie in cookies:
                    sess.cookies.set(cookie['name'], cookie['value'])
                return sess
            time.sleep(2)
            
        return None
        
    except Exception as e:
        st.error(f"Login Automation Error: {str(e)}")
        return None
    finally:
        if driver:
            try: driver.quit()
            except: pass

@st.cache_data(ttl=60)
def get_dashboard_data(_sess):
    if _sess is None: return None, None
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        r = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts", params={"includeShiftRoles": "true", "includeShiftUsers": "true"}, headers=headers)
        
        if r.status_code != 200: return None, None
        
        shifts = r.json()
        now_local = datetime.now(LOCAL_TZ)
        today = now_local.date()
        
        todays_shifts = []
        uids = set()
        for s in shifts:
            try:
                sd = datetime.fromisoformat(s['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                if sd.date() == today:
                    todays_shifts.append(s)
                    for r_role in s.get("roles", []):
                        for u in r_role.get("users", []): uids.add(u["id"])
            except: continue
                    
        service_map = {}
        def fetch_user_svc(uid):
            res = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime", headers=headers)
            return uid, res.json() if res.status_code == 200 else []

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(fetch_user_svc, uid) for uid in uids]
            for f in as_completed(futures):
                uid, data = f.result()
                service_map[uid] = data
        return todays_shifts, service_map
    except Exception as e:
        st.error(f"Data API Error: {e}")
        return None, None

# ─── App UI ───
with st.sidebar:
    st.title("🐾 Staff Access")
    if 'sess' not in st.session_state or st.session_state.sess is None:
        with st.form("auth_form"):
            user_email = st.text_input("Bloomerang Email")
            user_pw = st.text_input("Password", type="password")
            if st.form_submit_button("Log In"):
                if user_email and user_pw:
                    with st.spinner("Logging in to Bloomerang..."):
                        st.session_state.sess = authenticate_headless(user_email, user_pw)
                        if st.session_state.sess: st.rerun()
                        else: st.error("Authentication failed. Check your password or try again.")
                else:
                    st.warning("Please enter your credentials.")
    else:
        st.success("Session Active")
        if st.button("Refresh Board"): 
            st.cache_data.clear()
            st.rerun()
        if st.button("Log Out"): 
            st.session_state.sess = None
            st.rerun()

if st.session_state.get('sess'):
    now = datetime.now(LOCAL_TZ)
    st.title(f"Refuge Roster — {now.strftime('%A, %b %d')}")
    
    with st.spinner("Fetching today's schedule..."):
        shifts, svc_data = get_dashboard_data(st.session_state.sess)
    
    if shifts:
        cards = []
        for s in shifts:
            s_id = s['id']
            try:
                start = datetime.fromisoformat(s['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                end = datetime.fromisoformat(s['endDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            except: continue
            
            for role in s.get("roles", []):
                role_nm = role.get("eventRoleTexts", [{}])[0].get("eventRoleName", "Volunteer")
                for u in role.get("users", []):
                    uid = u['id']
                    name = f"{u['firstName']} {u['lastName']}"
                    recs = svc_data.get(uid, []) if svc_data else []
                    rec = next((r for r in recs if r.get('eventShiftId') == s_id and r.get('isActive')), None)
                    cin, cout = (rec.get('startTimestamp') if rec else None), (rec.get('endTimestamp') if rec else None)
                    
                    status, css = "Pending", "status-pending"
                    if cin and cout: status, css = "Checked Out", "status-checked-out"
                    elif cin:
                        status, css = ("Missing Clock-Out", "status-alert-red") if now > end + timedelta(minutes=10) else ("Checked In", "status-checked-in")
                    else:
                        if now > start + timedelta(minutes=10): status, css = "Late Check-In", "status-alert-red"
                        elif now >= start - timedelta(minutes=30): status, css = "Due Soon", "status-upcoming"

                    cards.append({
                        "time": start,
                        "html": f'<div class="shift-card {css}"><div class="shift-time">{start.strftime("%I:%M %p")} - {end.strftime("%I:%M %p")}</div><div class="shift-name">{name}</div><div class="shift-role">{role_nm}</div><span class="status-badge">{status}</span></div>'
                    })
        
        if cards:
            cards.sort(key=lambda x: x['time'])
            cols = st.columns(4)
            for i, c in enumerate(cards):
                with cols[i % 4]: st.markdown(c['html'], unsafe_allow_html=True)
        else:
            st.info("No more shifts scheduled for today.")
    elif shifts is None:
        st.warning("Session may have expired. Please log in again via the sidebar.")
                
    time.sleep(60)
    st.rerun()
else:
    st.info("Please log in using the sidebar to view today's volunteer roster.")

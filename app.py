import streamlit as st
import requests
import time
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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
    [data-theme="dark"] .shift-card {
        background-color: #1e2130;
        color: #ffffff;
    }
    .shift-time { font-size: 1rem; font-weight: 700; color: #666; margin-bottom: 0.3rem; }
    .shift-name { font-size: 1.4rem; font-weight: 800; margin-bottom: 0.1rem; }
    .shift-role { font-size: 0.85rem; text-transform: uppercase; color: #888; font-weight: 600; margin-bottom: 1rem; }
    
    .status-badge {
        padding: 0.4rem 0.8rem;
        border-radius: 20px;
        font-weight: 700;
        font-size: 0.75rem;
        text-transform: uppercase;
    }
    
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

# ─── Auth Logic ───
def authenticate_headless(email, password):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
    sess = requests.Session()
    
    try:
        # Use ChromeDriverManager but handle server pathing
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        
        driver.get(f"{BASE}/volunteer/#/login")
        wait = WebDriverWait(driver, 30)
        
        # Look for Cognito login fields
        # AWS Cognito often uses "username" and "password" as names
        email_el = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@type='email' or @name='username' or @id='email']")))
        pass_el = driver.find_element(By.XPATH, "//input[@type='password' or @name='password' or @id='password']")
        
        email_el.send_keys(email)
        pass_el.send_keys(password)
        
        # Click the Sign In button
        login_btn = driver.find_element(By.XPATH, "//button[@type='submit' or contains(text(), 'Sign In')]")
        login_btn.click()
        
        # Wait for redirect to the internal dashboard
        wait.until(EC.url_contains("dashboard"))
        time.sleep(3)
        
        for cookie in driver.get_cookies():
            sess.cookies.set(cookie['name'], cookie['value'])
            
        return sess
    except Exception as e:
        st.error(f"Authentication Error: {str(e)}")
        return None
    finally:
        try:
            driver.quit()
        except:
            pass

@st.cache_data(ttl=60)
def get_dashboard_data(_sess):
    try:
        r = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts", params={"includeShiftRoles": "true", "includeShiftUsers": "true"})
        if r.status_code != 200: return None, None
        
        shifts = r.json()
        now_local = datetime.now(LOCAL_TZ)
        today = now_local.date()
        
        todays_shifts = []
        uids = set()
        for s in shifts:
            sd = datetime.fromisoformat(s['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            if sd.date() == today:
                todays_shifts.append(s)
                for r_role in s.get("roles", []):
                    for u in r_role.get("users", []): uids.add(u["id"])
                    
        service_map = {}
        def fetch_user_svc(uid):
            res = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime")
            return uid, res.json() if res.status_code == 200 else []

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(fetch_user_svc, uid) for uid in uids]
            for f in as_completed(futures):
                uid, data = f.result()
                service_map[uid] = data
                
        return todays_shifts, service_map
    except Exception as e:
        st.error(f"Data Fetch Error: {e}")
        return None, None

# ─── App Layout ───
with st.sidebar:
    st.title("🐾 Staff Access")
    if 'sess' not in st.session_state or st.session_state.sess is None:
        with st.form("auth_form"):
            user_email = st.text_input("Bloomerang Email")
            user_pw = st.text_input("Password", type="password")
            if st.form_submit_button("Start Session"):
                if not user_email or not user_pw:
                    st.warning("Please enter credentials.")
                else:
                    with st.spinner("Logging in..."):
                        st.session_state.sess = authenticate_headless(user_email, user_pw)
                        if st.session_state.sess: st.rerun()
    else:
        st.success("Authorized")
        if st.button("Manual Refresh"): st.cache_data.clear()
        if st.button("Log Out"): 
            st.session_state.sess = None
            st.rerun()

if st.session_state.get('sess'):
    now = datetime.now(LOCAL_TZ)
    st.title(f"Refuge Roster — {now.strftime('%A, %b %d')}")
    
    with st.spinner("Syncing latest check-ins..."):
        shifts, svc_data = get_dashboard_data(st.session_state.sess)
    
    if shifts:
        cards = []
        for s in shifts:
            s_id = s['id']
            start = datetime.fromisoformat(s['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            end = datetime.fromisoformat(s['endDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            
            for role in s.get("roles", []):
                role_nm = role.get("eventRoleTexts", [{}])[0].get("eventRoleName", "Volunteer")
                for u in role.get("users", []):
                    uid = u['id']
                    name = f"{u['firstName']} {u['lastName']}"
                    
                    recs = svc_data.get(uid, []) if svc_data else []
                    rec = next((r for r in recs if r.get('eventShiftId') == s_id and r.get('isActive')), None)
                    
                    cin = rec.get('startTimestamp') if rec else None
                    cout = rec.get('endTimestamp') if rec else None
                    
                    status, css = "Pending", "status-pending"
                    
                    if cin and cout:
                        status, css = "Checked Out", "status-checked-out"
                    elif cin and not cout:
                        if now > end + timedelta(minutes=10):
                            status, css = "Missing Clock-Out", "status-alert-red"
                        else:
                            status, css = "Checked In", "status-checked-in"
                    else:
                        if now > start + timedelta(minutes=10):
                            status, css = "Late Check-In", "status-alert-red"
                        elif now >= start - timedelta(minutes=30):
                            status, css = "Due Soon", "status-upcoming"

                    cards.append({
                        "time": start,
                        "html": f"""
                        <div class="shift-card {css}">
                            <div class="shift-time">{start.strftime('%I:%M %p')} - {end.strftime('%I:%M %p')}</div>
                            <div class="shift-name">{name}</div>
                            <div class="shift-role">{role_nm}</div>
                            <span class="status-badge">{status}</span>
                        </div>
                        """
                    })
        
        if cards:
            cards.sort(key=lambda x: x['time'])
            # Responsive column logic
            cols = st.columns(4)
            for i, c in enumerate(cards):
                with cols[i % 4]:
                    st.markdown(c['html'], unsafe_allow_html=True)
        else:
            st.info("No shifts scheduled for today.")
                
    st.info("Live data auto-refreshes every 60 seconds.")
    time.sleep(60)
    st.rerun()
else:
    st.info("Use the sidebar to log in and view today's shifts.")

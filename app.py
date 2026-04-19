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

st.set_page_config(page_title="Lucky Dog Refuge Live Board", page_icon="🐾", layout="wide")

# ─── Custom CSS for the Marker Board UI ───
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
    .dark .shift-card {
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
    
    /* Logic Based Colors */
    .status-checked-in { border-left-color: #28a745 !important; background-color: #f1fbf3; }
    .status-checked-in .status-badge { background-color: #28a745; color: white; }
    
    .status-checked-out { border-left-color: #fd7e14 !important; background-color: #fffaf5; }
    .status-checked-out .status-badge { background-color: #fd7e14; color: white; }
    
    .status-alert-red { border-left-color: #dc3545 !important; background-color: #fff5f5; }
    .status-alert-red .status-badge { background-color: #dc3545; color: white; }
    
    .status-pending { border-left-color: #6c757d !important; background-color: #f8f9fa; }
    .status-pending .status-badge { background-color: #6c757d; color: white; }

    .status-upcoming { border-left-color: #007bff !important; background-color: #f0f7ff; }
    .status-upcoming .status-badge { background-color: #007bff; color: white; }
</style>
""", unsafe_allow_html=True)

# ─── Auth & API Logic ────────────────────────────────────────────────────────
def authenticate_headless(email, password):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    
    sess = requests.Session()
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    try:
        driver.get(f"{BASE}/volunteer/#/login")
        wait = WebDriverWait(driver, 20)
        
        # Cognito Login Flow
        email_el = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='email']")))
        pass_el = driver.find_element(By.XPATH, "//input[@type='password']")
        email_el.send_keys(email)
        pass_el.send_keys(password)
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        
        # Wait for redirect to dashboard
        wait.until(EC.url_contains("dashboard"))
        time.sleep(2)
        
        for cookie in driver.get_cookies():
            sess.cookies.set(cookie['name'], cookie['value'])
            
        return sess
    except Exception as e:
        st.error(f"Login Error: {e}")
        return None
    finally:
        driver.quit()

@st.cache_data(ttl=60)
def get_data(_sess):
    # Fetch Shifts
    r_shifts = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts", params={"includeShiftRoles": "true", "includeShiftUsers": "true"})
    if r_shifts.status_code != 200: return None, None
    
    all_shifts = r_shifts.json()
    now_local = datetime.now(LOCAL_TZ)
    today = now_local.date()
    
    todays_shifts = []
    uids = set()
    for s in all_shifts:
        sd = datetime.fromisoformat(s['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
        if sd.date() == today:
            todays_shifts.append(s)
            for r in s.get("roles", []):
                for u in r.get("users", []): uids.add(u["id"])
                
    # Fetch Service Times (Parallel)
    service_data = {}
    def fetch_user_st(uid):
        res = _sess.get(f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime")
        return uid, res.json() if res.status_code == 200 else []

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch_user_st, uid) for uid in uids]
        for f in as_completed(futures):
            uid, data = f.result()
            service_data[uid] = data
            
    return todays_shifts, service_data

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🐾 Staff Portal")
    if 'sess' not in st.session_state or st.session_state.sess is None:
        with st.form("login_form"):
            email = st.text_input("Email")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Log In"):
                with st.spinner("Authenticating..."):
                    st.session_state.sess = authenticate_headless(email, pw)
                    if st.session_state.sess: st.rerun()
    else:
        st.success("Session Active")
        if st.button("Refresh Data"): st.cache_data.clear()
        if st.button("Log Out"): 
            st.session_state.sess = None
            st.rerun()

# ─── Main Board ───────────────────────────────────────────────────────────────
if st.session_state.get('sess'):
    now = datetime.now(LOCAL_TZ)
    st.title(f"Volunteer Board — {now.strftime('%A, %B %d')}")
    
    shifts, st_map = get_data(st.session_state.sess)
    
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
                    
                    # Find clock record
                    recs = st_map.get(uid, [])
                    rec = next((r for r in recs if r.get('eventShiftId') == s_id and r.get('isActive')), None)
                    
                    has_in = rec.get('startTimestamp') if rec else None
                    has_out = rec.get('endTimestamp') if rec else None
                    
                    # Logic
                    status = "Pending"
                    css = "status-pending"
                    
                    if has_in and has_out:
                        status, css = "Checked Out", "status-checked-out"
                    elif has_in and not has_out:
                        if now > end + timedelta(minutes=10):
                            status, css = "Late Check-Out", "status-alert-red"
                        else:
                            status, css = "Checked In", "status-checked-in"
                    else:
                        if now > start + timedelta(minutes=10):
                            status, css = "Late Check-In", "status-alert-red"
                        elif now >= start - timedelta(minutes=30):
                            status, css = "Due Soon", "status-upcoming"

                    cards.append({
                        "time_val": start,
                        "html": f"""
                        <div class="shift-card {css}">
                            <div class="shift-time">{start.strftime('%I:%M %p')} - {end.strftime('%I:%M %p')}</div>
                            <div class="shift-name">{name}</div>
                            <div class="shift-role">{role_nm}</div>
                            <span class="status-badge">{status}</span>
                        </div>
                        """
                    })
        
        cards.sort(key=lambda x: x['time_val'])
        cols = st.columns(4)
        for i, c in enumerate(cards):
            with cols[i % 4]:
                st.markdown(c['html'], unsafe_allow_html=True)
                
    st.info("Auto-refreshing every minute...")
    time.sleep(60)
    st.rerun()
else:
    st.warning("Please log in using the sidebar to view the today's roster.")
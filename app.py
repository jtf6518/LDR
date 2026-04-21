import streamlit as st
import requests
import time
import os
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError as e:
    st.error(f"Missing dependencies: {e}")
    st.stop()

# ─── Configuration ────────────────────────────────────────────────────────────
BASE = "https://volunteer.bloomerang.co"
ORG_ID = 5269
EVENT_ID = 51764
LOCAL_TZ = ZoneInfo("America/New_York")

# Cache TTLs — tuned to minimize API load while keeping the board fresh
SHIFT_CACHE_TTL = 300     # shifts rarely change → 5 min
SERVICE_CACHE_TTL = 60    # clock-ins/outs surface here → 1 min
REFRESH_SECS = 120        # auto-rerun interval (UI refresh)

# Status thresholds
UPCOMING_MINUTES = 60     # within this many min of start → "Starting Soon"
LATE_OUT_MINUTES = 30     # clocked in but not out this long after shift end → "Missing Clock-Out"

st.set_page_config(page_title="Refuge Live Board", page_icon="🐾", layout="wide")

# ─── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap');

    html, body, [class*="css"] { font-family: 'Inter', system-ui, -apple-system, sans-serif; }

    .shift-card {
        padding: 1.25rem 1.35rem;
        border-radius: 14px;
        margin-bottom: 1rem;
        border-left: 6px solid #64748b;
        box-shadow: 0 4px 14px rgba(0,0,0,0.3);
        background: #1e2533;
        color: #f1f5f9;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    .shift-card:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(0,0,0,0.4); }

    .shift-time {
        font-size: 0.85rem; font-weight: 700; color: #94a3b8;
        margin-bottom: 0.55rem;
        font-family: 'JetBrains Mono', 'Courier New', monospace;
        letter-spacing: 0.02em;
    }
    .shift-name {
        font-size: 1.35rem; font-weight: 800;
        margin-bottom: 0.1rem; line-height: 1.15;
        color: #f8fafc !important;
    }
    .shift-role {
        font-size: 0.7rem; text-transform: uppercase;
        color: #94a3b8; font-weight: 700;
        margin-bottom: 0.9rem; letter-spacing: 0.1em;
    }
    .status-badge {
        padding: 0.4rem 0.85rem; border-radius: 20px;
        font-weight: 800; font-size: 0.7rem;
        text-transform: uppercase; display: inline-block;
        letter-spacing: 0.06em;
    }

    /* Status variants — all dark gradients + light text */

    /* Completed — purple */
    .status-completed {
        background: linear-gradient(135deg, #2d1e47 0%, #3a2862 100%);
        border-left-color: #a855f7;
    }
    .status-completed .status-badge { background: #a855f7; color: #1e1033; }

    /* On Shift (rare — only surfaces if cin/no-cout record appears) — green, pulsing */
    .status-checked-in {
        background: linear-gradient(135deg, #0f3d2b 0%, #1a5d44 100%);
        border-left-color: #10b981;
        animation: pulseGreen 2.2s ease-in-out infinite;
    }
    .status-checked-in .status-badge { background: #10b981; color: #052e1d; }
    @keyframes pulseGreen {
        0%, 100% { box-shadow: 0 4px 14px rgba(0,0,0,0.3), 0 0 0 0 rgba(16,185,129,0.3); }
        50%      { box-shadow: 0 4px 14px rgba(0,0,0,0.3), 0 0 0 6px rgba(16,185,129,0); }
    }

    /* In Progress — amber, neutral (honest: we can't confirm live state) */
    .status-in-progress {
        background: linear-gradient(135deg, #332719 0%, #4d3a28 100%);
        border-left-color: #eab308;
    }
    .status-in-progress .status-badge { background: #eab308; color: #1a0f00; }

    /* Missing Clock-Out — brighter amber */
    .status-alert-amber {
        background: linear-gradient(135deg, #3d2e0f 0%, #5c4515 100%);
        border-left-color: #f59e0b;
    }
    .status-alert-amber .status-badge { background: #f59e0b; color: #1a0f00; }

    /* No Show — red (only after shift is over) */
    .status-alert-red {
        background: linear-gradient(135deg, #3d1a1a 0%, #5d2626 100%);
        border-left-color: #ef4444;
    }
    .status-alert-red .status-badge { background: #ef4444; color: #2d0606; }

    /* Starting Soon — blue */
    .status-upcoming {
        background: linear-gradient(135deg, #1a2e4d 0%, #1e4473 100%);
        border-left-color: #3b82f6;
    }
    .status-upcoming .status-badge { background: #3b82f6; color: #0a1929; }

    /* Scheduled — neutral slate */
    .status-pending {
        background: linear-gradient(135deg, #1e2533 0%, #2a3344 100%);
        border-left-color: #64748b;
    }
    .status-pending .status-badge { background: #64748b; color: #0f172a; }

    .punch-box {
        margin-top: 10px; padding: 8px 12px;
        background: rgba(0,0,0,0.35); border-radius: 8px;
        font-family: 'JetBrains Mono', 'Courier New', monospace;
        font-size: 0.82rem; font-weight: 600;
        color: #e2e8f0;
        border: 1px solid rgba(255,255,255,0.06);
    }

    .meta-bar {
        background: #1e2533; padding: 0.65rem 1.1rem;
        border-radius: 10px; margin-bottom: 1rem;
        color: #cbd5e0; font-size: 0.82rem;
        display: flex; gap: 1.3rem; flex-wrap: wrap; align-items: center;
        border: 1px solid rgba(255,255,255,0.05);
    }
    .meta-bar .stat { display: flex; align-items: baseline; gap: 0.4rem; }
    .meta-bar .stat b { color: #f8fafc; font-size: 1rem; font-weight: 800; }
    .meta-bar .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .dot-green { background: #10b981; }
    .dot-purple { background: #a855f7; }
    .dot-blue { background: #3b82f6; }
    .dot-red { background: #ef4444; }
    .dot-amber { background: #eab308; }
    .dot-gray { background: #64748b; }

    .date-section-header {
        font-size: 1.15rem; font-weight: 800;
        color: #f8fafc;
        margin: 1.8rem 0 0.7rem 0;
        padding-bottom: 0.45rem;
        border-bottom: 2px solid #334155;
        letter-spacing: 0.02em;
    }
    .date-section-header .today-badge {
        display: inline-block;
        font-size: 0.65rem; font-weight: 800;
        padding: 2px 8px; border-radius: 10px;
        background: #10b981; color: #052e1d;
        margin-left: 0.6rem; vertical-align: middle;
        letter-spacing: 0.08em; text-transform: uppercase;
    }
</style>
""", unsafe_allow_html=True)

# ─── Auth ─────────────────────────────────────────────────────────────────────
def authenticate_headless(email, password):
    """Headless Chrome login. Returns auth dict or None."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    # Consistent UA reduces device-fingerprint variance across re-auths
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    if os.path.exists("/usr/bin/chromium"):
        options.binary_location = "/usr/bin/chromium"

    driver = None
    try:
        service = (Service("/usr/bin/chromedriver") if os.path.exists("/usr/bin/chromedriver")
                   else Service(ChromeDriverManager().install()))
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 15)

        driver.get(f"{BASE}/volunteer/#/login")
        time.sleep(3)

        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//input[@type='email' or @type='text']"))).send_keys(email)
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(translate(., 'NEXT', 'next'), 'next')]"))).click()

        time.sleep(2)
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//input[@type='password']"))).send_keys(password)
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(translate(., 'LOG IN', 'log in'), 'log in')]"))).click()

        try:
            wait.until(EC.presence_of_element_located(
                (By.XPATH, "//*[contains(text(), 'My Shifts') or contains(text(), 'Welcome')]")))
            time.sleep(2)
        except Exception:
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
        if driver:
            driver.quit()


def attempt_silent_reauth():
    """
    Re-login using cached credentials. Called automatically when safe_get_json
    gets a 401. Mutates st.session_state.auth_data in place so all in-flight
    references pick up the new cookies/token.
    """
    creds = st.session_state.get('credentials')
    if not creds:
        return None

    new_auth = authenticate_headless(creds['email'], creds['password'])
    if not new_auth:
        return None

    existing = st.session_state.get('auth_data')
    if existing:
        existing['sess'] = new_auth['sess']
        existing['token'] = new_auth['token']
    else:
        st.session_state.auth_data = new_auth

    st.session_state['last_reauth'] = time.time()
    st.cache_data.clear()  # cached responses may have been built with stale auth
    return st.session_state.auth_data


def safe_get_json(auth, url, params=None, _retried=False):
    """GET with automatic silent re-auth on 401."""
    if not auth:
        return "NO_AUTH"

    sess = auth['sess']
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': f'{BASE}/volunteer/',
    }
    if auth.get('token'):
        headers['Authorization'] = f"Bearer {auth['token']}"

    try:
        r = sess.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 401 and not _retried:
            new_auth = attempt_silent_reauth()
            if new_auth:
                return safe_get_json(new_auth, url, params, _retried=True)
            return "AUTH_EXPIRED"
        if r.status_code != 200:
            return f"ERR_{r.status_code}"
        return r.json()
    except Exception as e:
        return f"ERR_REQ: {str(e)}"


# ─── Data Fetching ────────────────────────────────────────────────────────────
@st.cache_data(ttl=SHIFT_CACHE_TTL)
def get_shifts(_auth):
    """All shifts for the event. Returns raw list or error string. Cached 5 min."""
    return safe_get_json(_auth,
        f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts",
        {"includeShiftRoles": "true",
         "includeShiftUsers": "true",
         "take": 500,
         "includePast": "true"})


@st.cache_data(ttl=SERVICE_CACHE_TTL)
def get_service_times(_auth, uids_tuple):
    """Parallel fetch of serviceTime for each uid. Cached 60 sec."""
    uids = list(uids_tuple)
    result = {}
    def fetch(uid):
        r = safe_get_json(_auth,
            f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime")
        return uid, r if isinstance(r, list) else []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch, uid) for uid in uids]
        for f in as_completed(futures):
            uid, data = f.result()
            result[uid] = data
    return result


# ─── Processing ───────────────────────────────────────────────────────────────
def build_roster(shifts_raw, target_dates):
    """Flatten shifts into (person, shift) records for the given date set."""
    if not isinstance(shifts_raw, list):
        return [], []

    roster = []
    uids = set()
    seen = set()

    for shift in shifts_raw:
        try:
            s_start = datetime.fromisoformat(
                shift['startDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            s_end = datetime.fromisoformat(
                shift['endDate'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
        except Exception:
            continue

        if s_start.date() not in target_dates:
            continue

        sid = shift['id']
        for role in shift.get('roles', []):
            r_name = "Volunteer"
            texts = role.get('eventRoleTexts') or []
            if texts and isinstance(texts, list):
                r_name = texts[0].get('eventRoleName', 'Volunteer')

            for user in role.get('users', []):
                uid = user.get('id')
                if not uid:
                    continue
                key = (sid, uid)
                if key in seen:
                    continue
                seen.add(key)

                fname = (user.get('firstName') or '').strip()
                lname = (user.get('lastName') or '').strip()
                if not fname or fname.lower() in ('none', 'volunteer'):
                    fname = 'Unknown'
                    lname = f'(ID: {uid})'

                roster.append({
                    'uid': uid, 'sid': sid,
                    'fname': fname, 'lname': lname,
                    'role': r_name,
                    'start': s_start, 'end': s_end,
                })
                uids.add(uid)

    return roster, list(uids)


def find_punch(user_punches, shift_info, t_date):
    """
    Pick the most relevant serviceTime record for this shift on this date.
    Priority: exact shift+date match (real punches) > exact shift (manager-fix)
    > fuzzy time match within ±90 min.
    """
    if not user_punches:
        return None

    sid = shift_info['sid']
    s_start, s_end = shift_info['start'], shift_info['end']

    exact_real, exact_fixed, fuzzy = [], [], []

    for p in user_punches:
        start_raw = p.get('startTimestamp')
        end_raw = p.get('endTimestamp')
        same_shift = p.get('eventShiftId') == sid

        # Manager-fix (both null) — only accept for exact shift + day match
        if not start_raw and not end_raw:
            day = p.get('dayDate', '')
            if same_shift and day.startswith(t_date.isoformat()):
                exact_fixed.append(p)
            continue

        if not start_raw:
            continue

        try:
            p_start = datetime.fromisoformat(
                start_raw.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
        except Exception:
            continue

        if p_start.date() != t_date:
            continue

        if same_shift:
            exact_real.append(p)
        elif s_start - timedelta(minutes=90) <= p_start <= s_end + timedelta(minutes=90):
            fuzzy.append(p)

    # Prefer exact-real > exact-fixed > fuzzy. Within a bucket, newest wins.
    for bucket in (exact_real, exact_fixed, fuzzy):
        if bucket:
            return max(bucket, key=lambda p: p.get('startTimestamp', '') or p.get('dayDate', ''))

    return None


def classify(shift_info, punch, now):
    """
    Honest status derivation — no claims about live state we can't verify.
    Returns (status_label, css_class, clock_in_dt, clock_out_dt).
    """
    start, end = shift_info['start'], shift_info['end']

    if punch:
        cin_raw = punch.get('startTimestamp')
        cout_raw = punch.get('endTimestamp')
        cin = (datetime.fromisoformat(cin_raw.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
               if cin_raw else None)
        cout = (datetime.fromisoformat(cout_raw.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                if cout_raw else None)

        # Manager-fix entry — hours credited manually
        if not cin and not cout:
            return 'Completed (Fixed)', 'status-completed', None, None

        if cin and cout:
            return 'Completed', 'status-completed', cin, cout

        if cin and not cout:
            if now > end + timedelta(minutes=LATE_OUT_MINUTES):
                return 'Missing Clock-Out', 'status-alert-amber', cin, None
            return 'On Shift', 'status-checked-in', cin, None

    # No punch record visible
    if now < start - timedelta(minutes=UPCOMING_MINUTES):
        return 'Scheduled', 'status-pending', None, None
    if now < start:
        return 'Starting Soon', 'status-upcoming', None, None
    if now <= end + timedelta(minutes=LATE_OUT_MINUTES):
        # Shift window is active — serviceTime doesn't surface live clock-ins
        return 'In Progress', 'status-in-progress', None, None
    # Shift is over and nothing was recorded
    return 'No Show', 'status-alert-red', None, None


# ─── Rendering ────────────────────────────────────────────────────────────────
def render_meta_bar(counts, total, sync_time=None):
    done = counts.get('Completed', 0) + counts.get('Completed (Fixed)', 0)
    on = counts.get('On Shift', 0)
    inprog = counts.get('In Progress', 0)
    up = counts.get('Starting Soon', 0)
    sched = counts.get('Scheduled', 0)
    miss = counts.get('Missing Clock-Out', 0)
    ns = counts.get('No Show', 0)
    alerts = miss + ns

    parts = [f'<div class="stat"><b>{total}</b> shifts</div>']
    if done:   parts.append(f'<div class="stat"><span class="dot dot-purple"></span><b>{done}</b> completed</div>')
    if on:     parts.append(f'<div class="stat"><span class="dot dot-green"></span><b>{on}</b> on shift</div>')
    if inprog: parts.append(f'<div class="stat"><span class="dot dot-amber"></span><b>{inprog}</b> in progress</div>')
    if up:     parts.append(f'<div class="stat"><span class="dot dot-blue"></span><b>{up}</b> starting soon</div>')
    if sched:  parts.append(f'<div class="stat"><span class="dot dot-gray"></span><b>{sched}</b> scheduled</div>')
    if alerts: parts.append(f'<div class="stat"><span class="dot dot-red"></span><b>{alerts}</b> needs attention</div>')
    if sync_time:
        parts.append(f'<div class="stat" style="margin-left:auto; color:#64748b;">'
                     f'Last sync: {sync_time.strftime("%I:%M:%S %p")}</div>')

    st.markdown(f'<div class="meta-bar">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_card(card):
    v = card['v']
    status, css = card['status'], card['css']
    cin, cout = card['cin'], card['cout']
    full_name = f"{v['fname']} {v['lname']}".strip()

    punch_box = ""
    if cin or cout:
        cin_str = cin.strftime('%I:%M %p') if cin else '--'
        cout_str = cout.strftime('%I:%M %p') if cout else '--'
        punch_box = f'<div class="punch-box">🕒 In: {cin_str} → Out: {cout_str}</div>'
    elif status == 'Completed (Fixed)':
        punch_box = '<div class="punch-box">✎ Hours manually credited</div>'

    return f"""
    <div class="shift-card {css}">
        <div class="shift-time">{v['start'].strftime('%I:%M %p')} — {v['end'].strftime('%I:%M %p')}</div>
        <div class="shift-name">{full_name}</div>
        <div class="shift-role">{v['role']}</div>
        {punch_box}
        <div style="margin-top:12px;"><span class="status-badge">{status}</span></div>
    </div>
    """


# ─── UI ───────────────────────────────────────────────────────────────────────
if 'auth_data' not in st.session_state:
    st.session_state.auth_data = None
if 'credentials' not in st.session_state:
    st.session_state.credentials = None

today = datetime.now(LOCAL_TZ).date()

with st.sidebar:
    st.title("🐾 Staff Access")

    if st.session_state.auth_data is None:
        with st.form("auth_form"):
            u = st.text_input("Email")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Log In"):
                with st.spinner("Logging in..."):
                    auth = authenticate_headless(u, p)
                if auth:
                    st.session_state.auth_data = auth
                    st.session_state.credentials = {'email': u, 'password': p}
                    st.rerun()
    else:
        st.success("Connected")
        last_reauth = st.session_state.get('last_reauth')
        if last_reauth:
            ago_min = int((time.time() - last_reauth) / 60)
            if ago_min < 60:
                st.caption(f"🔑 Auth refreshed {ago_min} min ago")

        st.divider()
        st.markdown("**Date range**")
        default_range = (today, today + timedelta(days=2))
        date_sel = st.date_input(
            "Dates",
            value=st.session_state.get('_date_selection', default_range),
            help="Pick a single day or a range",
            label_visibility="collapsed",
            key='date_input_widget',
        )

        # Quick presets
        c1, c2, c3 = st.columns(3)
        if c1.button("Today", use_container_width=True):
            st.session_state['_date_selection'] = (today, today)
            st.rerun()
        if c2.button("Next 3d", use_container_width=True):
            st.session_state['_date_selection'] = (today, today + timedelta(days=2))
            st.rerun()
        if c3.button("Week", use_container_width=True):
            st.session_state['_date_selection'] = (today, today + timedelta(days=6))
            st.rerun()

        st.divider()
        if st.button("🔄 Refresh Now", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        if st.button("Logout", use_container_width=True):
            for k in ('auth_data', 'credentials', 'last_reauth', '_date_selection'):
                st.session_state.pop(k, None)
            st.rerun()
        st.caption(f"Auto-refreshes every {REFRESH_SECS}s")

# ─── Main ─────────────────────────────────────────────────────────────────────
if not st.session_state.auth_data:
    st.info("Staff Login Required.")
    st.stop()

# Normalize the date selection into (start, end)
if isinstance(date_sel, tuple) and len(date_sel) == 2:
    start_date, end_date = date_sel
elif isinstance(date_sel, (list, tuple)) and len(date_sel) == 1:
    start_date = end_date = date_sel[0]
elif isinstance(date_sel, date):
    start_date = end_date = date_sel
else:
    start_date = end_date = today

if start_date > end_date:
    start_date, end_date = end_date, start_date

dates_in_range = []
d = start_date
while d <= end_date:
    dates_in_range.append(d)
    d += timedelta(days=1)

# Title
if len(dates_in_range) == 1:
    title_str = dates_in_range[0].strftime('%A, %B %d')
else:
    title_str = f"{dates_in_range[0].strftime('%b %d')} — {dates_in_range[-1].strftime('%b %d, %Y')}"
st.title(f"🐾 Refuge Roster — {title_str}")

# Fetch shifts
with st.spinner("Syncing Bloomerang..."):
    shifts_raw = get_shifts(st.session_state.auth_data)

if isinstance(shifts_raw, str):
    if shifts_raw == "AUTH_EXPIRED":
        st.session_state.auth_data = None
        st.error("Session expired. Please log in again.")
        st.stop()
    st.error(f"API Error: {shifts_raw}")
    st.stop()

# Build roster for date range
roster, uids = build_roster(shifts_raw, set(dates_in_range))

if not roster:
    st.info(f"No volunteers scheduled for {title_str}.")
else:
    now = datetime.now(LOCAL_TZ)
    # Only fetch serviceTime if at least one date in range is today or earlier.
    # Future-only ranges don't need it — no records exist yet.
    need_service = any(d <= now.date() for d in dates_in_range)
    if need_service:
        with st.spinner("Syncing shift history..."):
            punches = get_service_times(
                st.session_state.auth_data, tuple(sorted(uids)))
    else:
        punches = {uid: [] for uid in uids}

    # Group roster by date
    by_date = {}
    for v in roster:
        d_key = v['start'].date()
        by_date.setdefault(d_key, []).append(v)

    # Render each date section
    for section_idx, date_key in enumerate(sorted(by_date.keys())):
        shifts_for_date = by_date[date_key]
        is_today = (date_key == now.date())

        cards = []
        counts = {}
        for v in shifts_for_date:
            user_punches = punches.get(v['uid'], []) if need_service else []
            p = find_punch(user_punches, v, date_key)
            status, css, cin, cout = classify(v, p, now)
            counts[status] = counts.get(status, 0) + 1
            cards.append({'v': v, 'status': status, 'css': css, 'cin': cin, 'cout': cout})

        cards.sort(key=lambda c: c['v']['start'])

        # Section header
        today_badge = '<span class="today-badge">Today</span>' if is_today else ''
        st.markdown(
            f'<div class="date-section-header">{date_key.strftime("%A, %B %d")}'
            f'{today_badge}</div>',
            unsafe_allow_html=True
        )
        # Show sync time only on the first section to avoid clutter
        render_meta_bar(counts, len(cards),
                        sync_time=now if section_idx == 0 else None)

        # Card grid
        cols = st.columns(4)
        for idx, card in enumerate(cards):
            with cols[idx % 4]:
                st.markdown(render_card(card), unsafe_allow_html=True)

# ─── Auto-refresh ─────────────────────────────────────────────────────────────
time.sleep(REFRESH_SECS)
st.rerun()

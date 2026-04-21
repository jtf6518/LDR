import streamlit as st
import requests
import time
import os
from datetime import datetime, timedelta, date, timezone
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
        s_start = _parse_bloomerang_ts(shift.get('startDate'))
        s_end = _parse_bloomerang_ts(shift.get('endDate'))
        if s_start is None or s_end is None:
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


def _parse_bloomerang_ts(raw):
    """
    Parse a Bloomerang timestamp string. Bloomerang's API returns timestamps
    either as `2026-04-11T18:01:23.000Z` (explicit UTC) or as bare
    `2026-04-11T18:01:23` (implicit UTC, no suffix). Python's fromisoformat
    with a naive string returns a naive datetime, which .astimezone() then
    misinterprets using the server's local zone. We normalize by:
      1. Stripping Z and attaching explicit UTC
      2. If still naive after parsing, attaching UTC anyway
      3. Converting to LOCAL_TZ

    Returns a timezone-aware datetime in LOCAL_TZ, or None on failure.
    """
    if not raw:
        return None
    try:
        s = raw.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ)
    except Exception:
        return None


def assign_punches(user_shifts, user_punches, t_date):
    """
    Given one volunteer's shifts and punches for a single date, decide which
    punch (if any) belongs to which shift.

    Per the Bloomerang API spec (serviceTime schema):
      - startTimestamp / endTimestamp are in UTC with ".000Z" suffix
      - dayDate is a local-timezone calendar date (YYYY-MM-DD)
      - isActive=true means the record is current data. isActive=false means
        this record was superseded by a later edit (see supersededBy).
        Filtering to active-only is critical — otherwise we pick the stale
        pre-edit version and misreport clock times.
      - checkinTypeId:     1 = general check-in,     2 = shift clock-in
      - serviceTimeTypeId: 1 = realtime (clock-in),  2 = manually entered

    Algorithm:
      1. Drop superseded records.
      2. Exact shift-id pass: punch.eventShiftId matches a scheduled shift.
      3. Proximity fallback for the rare punch with no eventShiftId.
      4. One-to-one assignment — each punch claims at most one shift.

    Returns dict: {str(shift_id) -> punch_record}.
    """
    if not user_punches or not user_shifts:
        return {}

    iso_day = t_date.isoformat()

    # Drop superseded records. isActive can be True, 1, None (missing), or
    # explicitly False/0. Scraper treats truthy/missing as active, so we do
    # the same — only explicit false is excluded.
    active_punches = []
    for p in user_punches:
        is_active = p.get('isActive')
        if is_active is False or is_active == 0:
            continue
        active_punches.append(p)

    # Step 1 — filter to today's candidates. Normalize each to an anchor time.
    candidates = []
    for p in active_punches:
        start_raw = p.get('startTimestamp')

        if start_raw:
            anchor = _parse_bloomerang_ts(start_raw)
            if anchor is None:
                continue
            if anchor.date() != t_date:
                continue
        else:
            # Manually entered record — no timestamps, just dayDate.
            # dayDate is already in local timezone per the spec.
            day = p.get('dayDate', '')
            if not day.startswith(iso_day):
                continue
            anchor = None

        candidates.append({'punch': p, 'anchor': anchor})

    if not candidates:
        return {}

    # Step 2 — exact shift-id match. The API stamps each clock-in with the
    # shift it was logged against. When present, this is authoritative.
    assigned = {}
    shifts_by_sid = {str(s['sid']): s for s in user_shifts}
    candidates_remaining = []

    for c in candidates:
        p_sid = c['punch'].get('eventShiftId')
        if p_sid is not None and str(p_sid) in shifts_by_sid:
            key = str(p_sid)
            existing = assigned.get(key)
            if existing is None:
                assigned[key] = c['punch']
            else:
                # Prefer realtime punch over manually-entered.
                if c['punch'].get('startTimestamp') and not existing.get('startTimestamp'):
                    assigned[key] = c['punch']
        else:
            candidates_remaining.append(c)

    # Step 3 — proximity fallback for punches without an eventShiftId.
    # (Rare edge case — most real punches carry their shift id.)
    unclaimed_shifts = [s for s in user_shifts if str(s['sid']) not in assigned]

    scored = []
    for c in candidates_remaining:
        if c['anchor'] is None:
            continue
        for s in unclaimed_shifts:
            if c['anchor'] < s['start'] - timedelta(minutes=30):
                continue
            if c['anchor'] > s['end'] + timedelta(minutes=60):
                continue
            distance = abs((c['anchor'] - s['start']).total_seconds())
            scored.append((distance, c, s))

    scored.sort(key=lambda t: t[0])

    used_punch_ids = set()
    for distance, c, s in scored:
        sid_key = str(s['sid'])
        if sid_key in assigned:
            continue
        pid = id(c['punch'])
        if pid in used_punch_ids:
            continue
        assigned[sid_key] = c['punch']
        used_punch_ids.add(pid)

    return assigned


def find_punch(user_punches, shift_info, t_date, _cache=None):
    """
    Return the punch for this shift on this date, or None.

    This is a thin wrapper around assign_punches; the real work is one-to-one
    allocation across a user's whole day. A cache keyed by (uid, date) avoids
    re-running the allocator for each shift belonging to the same user.
    """
    return None  # overridden below — this function is now only called with pre-computed results


def classify(shift_info, punch, now):
    """
    Honest status derivation — no claims about live state we can't verify.
    Returns (status_label, css_class, clock_in_dt, clock_out_dt).
    """
    start, end = shift_info['start'], shift_info['end']

    if punch:
        cin_raw = punch.get('startTimestamp')
        cout_raw = punch.get('endTimestamp')
        cin = _parse_bloomerang_ts(cin_raw)
        cout = _parse_bloomerang_ts(cout_raw)

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


def render_card(card, debug=False):
    v = card['v']
    status, css = card['status'], card['css']
    cin, cout = card['cin'], card['cout']
    full_name = f"{v['fname']} {v['lname']}".strip()

    if cin or cout:
        cin_str = cin.strftime('%I:%M %p') if cin else '--'
        cout_str = cout.strftime('%I:%M %p') if cout else '--'
        punch_box = f'<div class="punch-box">🕒 In: {cin_str} → Out: {cout_str}</div>'
    elif status == 'Completed (Fixed)':
        punch_box = '<div class="punch-box">✎ Hours manually credited</div>'
    else:
        punch_box = ''

    time_str = f"{v['start'].strftime('%I:%M %p')} — {v['end'].strftime('%I:%M %p')}"

    # Debug footer: show shift's own ID and the matched punch's shift ID
    debug_footer = ''
    if debug:
        matched_sid = card.get('matched_sid', 'none')
        via_proximity = card.get('matched_via_proximity', False)
        available = card.get('available_sids', [])

        if via_proximity:
            marker = '≈ matched by time proximity (punch had no eventShiftId)'
        elif matched_sid == v['sid'] or str(matched_sid) == str(v['sid']):
            marker = '✓ exact id match'
        elif matched_sid == 'none':
            marker = '∅ no match'
        else:
            marker = '⚠︎ MISMATCH'

        shift_sid_display = f"{v['sid']} ({type(v['sid']).__name__})"
        match_display = f"{matched_sid} ({type(matched_sid).__name__})"

        avail_display = ''
        if matched_sid == 'none' and available:
            pairs = [f"{s} ({type(s).__name__})" for s in available]
            avail_display = f'<br/>today\'s punches had eventShiftIds: {", ".join(pairs)}'

        debug_footer = (
            f'<div style="margin-top:8px; padding:6px 10px; background:rgba(0,0,0,0.4); '
            f'border-radius:6px; font-family:JetBrains Mono,monospace; font-size:0.68rem; '
            f'color:#94a3b8; line-height:1.4;">shift.id={shift_sid_display}<br/>'
            f'matched={match_display}<br/>{marker}{avail_display}</div>'
        )

    # Single-line HTML — no indentation or blank lines that would confuse
    # Streamlit's markdown-to-HTML passthrough into rendering fragments as text.
    return (
        f'<div class="shift-card {css}">'
        f'<div class="shift-time">{time_str}</div>'
        f'<div class="shift-name">{full_name}</div>'
        f'<div class="shift-role">{v["role"]}</div>'
        f'{punch_box}'
        f'<div style="margin-top:12px;"><span class="status-badge">{status}</span></div>'
        f'{debug_footer}'
        f'</div>'
    )


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
        st.session_state['debug_mode'] = st.checkbox(
            "🔬 Show shift IDs on cards",
            value=st.session_state.get('debug_mode', False),
            help="Displays each shift's id and the eventShiftId of its matched punch, "
                 "so you can verify shift→punch attribution.",
        )
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
    # Render each date section
    for section_idx, date_key in enumerate(sorted(by_date.keys())):
        shifts_for_date = by_date[date_key]
        is_today = (date_key == now.date())

        # Run punch allocation once per user per date (one-to-one matching)
        allocation_by_uid = {}  # uid -> {sid (str) -> punch}
        shifts_by_uid = {}
        for v in shifts_for_date:
            shifts_by_uid.setdefault(v['uid'], []).append(v)
        for uid, user_shifts in shifts_by_uid.items():
            user_punches = punches.get(uid, []) if need_service else []
            allocation_by_uid[uid] = assign_punches(user_shifts, user_punches, date_key)

        cards = []
        counts = {}
        for v in shifts_for_date:
            alloc = allocation_by_uid.get(v['uid'], {})
            p = alloc.get(str(v['sid']))
            status, css, cin, cout = classify(v, p, now)
            counts[status] = counts.get(status, 0) + 1

            # Debug: list all today's eventShiftIds in this user's punches
            available_sids = []
            if st.session_state.get('debug_mode') and need_service:
                user_punches = punches.get(v['uid'], [])
                for pp in user_punches:
                    st_raw = pp.get('startTimestamp')
                    if st_raw:
                        dt = _parse_bloomerang_ts(st_raw)
                        if dt and dt.date() == date_key:
                            available_sids.append(pp.get('eventShiftId'))

            cards.append({
                'v': v, 'status': status, 'css': css, 'cin': cin, 'cout': cout,
                'matched_sid': p.get('eventShiftId') if p else 'none',
                'available_sids': available_sids,
                'matched_via_proximity': p is not None and p.get('eventShiftId') is None,
            })

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
                st.markdown(render_card(card, debug=st.session_state.get('debug_mode', False)),
                            unsafe_allow_html=True)

# ─── Auto-refresh ─────────────────────────────────────────────────────────────
time.sleep(REFRESH_SECS)
st.rerun()

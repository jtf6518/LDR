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
CACHE_TTL = 30        # seconds - short so check-ins surface quickly
REFRESH_SECS = 120    # auto-rerun interval (2 minutes)

# Alert thresholds
LATE_IN_MINUTES  = 10   # no clock-in this long after shift start → No Show / Late
LATE_OUT_MINUTES = 30   # still not clocked out this long after shift end → Late Out

st.set_page_config(page_title="Refuge Live Board", page_icon="🐾", layout="wide")

# ─── Custom CSS — solid dark theme, no data-theme reliance ────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap');

    html, body, [class*="css"] { font-family: 'Inter', system-ui, -apple-system, sans-serif; }

    /* Base card — explicit dark background + light text, works in any theme */
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
        font-size: 0.85rem;
        font-weight: 700;
        color: #94a3b8;
        margin-bottom: 0.55rem;
        font-family: 'JetBrains Mono', 'Courier New', monospace;
        letter-spacing: 0.02em;
    }

    .shift-name {
        font-size: 1.35rem;
        font-weight: 800;
        margin-bottom: 0.1rem;
        line-height: 1.15;
        color: #f8fafc !important;   /* always light — never inherit dark */
    }

    .shift-role {
        font-size: 0.7rem;
        text-transform: uppercase;
        color: #94a3b8;
        font-weight: 700;
        margin-bottom: 0.9rem;
        letter-spacing: 0.1em;
    }

    .status-badge {
        padding: 0.4rem 0.85rem;
        border-radius: 20px;
        font-weight: 800;
        font-size: 0.7rem;
        text-transform: uppercase;
        display: inline-block;
        letter-spacing: 0.06em;
    }

    /* ── Status variants — ALL use solid dark gradients + light text ── */

    /* On Shift — bright green, pulsing left border */
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

    /* Completed — purple */
    .status-completed {
        background: linear-gradient(135deg, #2d1e47 0%, #3a2862 100%);
        border-left-color: #a855f7;
    }
    .status-completed .status-badge { background: #a855f7; color: #1e1033; }

    /* Red alerts — No Show / Late Out */
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

    /* Scheduled (later today) — neutral slate */
    .status-pending {
        background: linear-gradient(135deg, #1e2533 0%, #2a3344 100%);
        border-left-color: #64748b;
    }
    .status-pending .status-badge { background: #64748b; color: #0f172a; }

    .punch-box {
        margin-top: 10px;
        padding: 8px 12px;
        background: rgba(0,0,0,0.35);
        border-radius: 8px;
        font-family: 'JetBrains Mono', 'Courier New', monospace;
        font-size: 0.82rem;
        font-weight: 600;
        color: #e2e8f0;
        border: 1px solid rgba(255,255,255,0.06);
    }

    /* Meta / summary bar */
    .meta-bar {
        background: #1e2533;
        padding: 0.75rem 1.2rem;
        border-radius: 10px;
        margin-bottom: 1.2rem;
        color: #cbd5e0;
        font-size: 0.85rem;
        display: flex;
        gap: 1.5rem;
        flex-wrap: wrap;
        align-items: center;
        border: 1px solid rgba(255,255,255,0.05);
    }
    .meta-bar .stat { display: flex; align-items: baseline; gap: 0.4rem; }
    .meta-bar .stat b { color: #f8fafc; font-size: 1.1rem; font-weight: 800; }
    .meta-bar .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .dot-green { background: #10b981; }
    .dot-purple { background: #a855f7; }
    .dot-blue { background: #3b82f6; }
    .dot-red { background: #ef4444; }
    .dot-gray { background: #64748b; }

    .section-header {
        font-size: 0.85rem;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: #94a3b8;
        margin: 1.8rem 0 0.8rem 0;
        padding-bottom: 0.4rem;
        border-bottom: 1px solid rgba(255,255,255,0.08);
    }
</style>
""", unsafe_allow_html=True)


# ─── Auth ─────────────────────────────────────────────────────────────────────
def authenticate_headless(email, password):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    if os.path.exists("/usr/bin/chromium"):
        options.binary_location = "/usr/bin/chromium"

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
        if r.status_code == 401:
            return "AUTH_EXPIRED"
        if r.status_code != 200:
            return f"ERR_{r.status_code}: {r.text[:150]}"
        return r.json()
    except Exception as e:
        return f"ERR_REQ: {str(e)}"


# ─── Punch Matching ───────────────────────────────────────────────────────────
def find_punch_for_shift(user_punches, shift, t_date):
    """
    Pick the most relevant service-time record for a given shift TODAY.

    Priority (highest first):
      1. ACTIVE record (startTimestamp set, endTimestamp null) matching eventShiftId
         → Someone is clocked in RIGHT NOW for this exact shift.
      2. ACTIVE record today within ±90 min of shift window
         → Clocked in without specifying the shift, but time matches.
      3. COMPLETED record matching eventShiftId (today)
      4. COMPLETED record today within ±90 min of shift window
    Manager-fix entries (both timestamps null) are ignored — they provide no
    clock-in/out visualization value for a live board.
    """
    if not user_punches:
        return None

    sid = shift['sid']
    s_start = shift['start']
    s_end = shift['end']
    window_start = s_start - timedelta(minutes=90)
    window_end = s_end + timedelta(minutes=90)

    exact_active, exact_done = [], []
    fuzzy_active, fuzzy_done = [], []

    for p in user_punches:
        start_raw = p.get('startTimestamp')
        end_raw = p.get('endTimestamp')

        # Skip manager-fix entries (no real punches)
        if not start_raw:
            continue

        try:
            p_start = datetime.fromisoformat(start_raw.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
        except Exception:
            continue

        # Must be TODAY in local TZ
        if p_start.date() != t_date:
            continue

        is_active = not end_raw
        matches_shift = p.get('eventShiftId') == sid
        in_window = window_start <= p_start <= window_end

        if matches_shift:
            (exact_active if is_active else exact_done).append(p)
        elif in_window:
            (fuzzy_active if is_active else fuzzy_done).append(p)

    # Return in strict priority order; within a bucket, most recent startTimestamp wins
    for bucket in (exact_active, fuzzy_active, exact_done, fuzzy_done):
        if bucket:
            return max(bucket, key=lambda p: p.get('startTimestamp', ''))

    return None


# ─── Data Fetching ────────────────────────────────────────────────────────────
@st.cache_data(ttl=CACHE_TTL)
def get_dashboard_data(_auth_dict, target_date_obj):
    if not _auth_dict:
        return None, None, None

    # 1. Shifts (with users embedded) — primary roster source
    shifts_params = {
        "includeShiftRoles": "true",
        "includeShiftUsers": "true",
        "take": 500,
        "includePast": "true",
    }
    s_raw = safe_get_json(_auth_dict,
                          f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/shifts",
                          shifts_params)

    if isinstance(s_raw, str):
        return s_raw, None, None
    if not s_raw:
        return "ERR_EMPTY", None, None

    shift_defs = {s['id']: s for s in s_raw}

    # 2. Try enrollments/attendance as secondary sources (often 403 — silently skip)
    enrollments = safe_get_json(_auth_dict,
                                f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/enrollments",
                                {"take": 500})
    if isinstance(enrollments, str):
        enrollments = []

    attendance = safe_get_json(_auth_dict,
                               f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/attendance",
                               {"take": 500})
    if isinstance(attendance, str):
        attendance = []

    # 2b. Try the event-scoped /presence endpoint — this is the ONLY place the
    # Bloomerang API exposes live check-in / clock-in state. Returns an array of
    # presence records with {status, eventShiftId, eventUserAccountId, username,
    # dateCreated}. May return 403 for volunteer-level tokens — if so, we fall
    # back to serviceTime-only detection (which misses active check-ins).
    presence_raw = safe_get_json(
        _auth_dict,
        f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/presence"
    )
    if isinstance(presence_raw, list):
        presence_data = presence_raw
        presence_error = None
    else:
        presence_data = []
        presence_error = presence_raw  # keep the error string for debug panel

    raw_people = []
    uids = set()
    seen_keys = set()

    def process_person(item):
        uid = item.get('userId') or item.get('id')
        sid = item.get('eventShiftId') or item.get('shiftId')
        if not uid or not sid:
            return

        s_def = shift_defs.get(sid)

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
            if not start_raw:
                return
            s_start = datetime.fromisoformat(start_raw.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
            s_end = (datetime.fromisoformat(end_raw.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                     if end_raw else s_start + timedelta(hours=1))
            r_name = item.get('eventRoleName') or item.get('roleName') or "Volunteer"

        if s_start.date() != target_date_obj:
            return

        key = f"{sid}-{uid}"
        if key in seen_keys:
            return

        raw_people.append({
            'uid': uid,
            'sid': sid,
            'fname': (item.get('firstName') or '').strip(),
            'lname': (item.get('lastName') or '').strip(),
            'role': r_name,
            'start': s_start,
            'end': s_end,
        })
        uids.add(uid)
        seen_keys.add(key)

    for e in enrollments:
        process_person(e)
    for a in attendance:
        process_person(a)
    # Primary source: users embedded in shift roles
    for sid, sdef in shift_defs.items():
        for role in sdef.get('roles', []):
            for user in role.get('users', []):
                process_person({
                    'userId': user.get('id'),
                    'eventShiftId': sid,
                    'firstName': user.get('firstName'),
                    'lastName': user.get('lastName'),
                    'eventRoleId': role.get('id'),
                })

    # 3. Fetch service time for every user on today's roster (parallel)
    # Try BOTH the org-scoped and event-scoped endpoints — they may return
    # different subsets (e.g. in-progress check-ins might only appear in one).
    punch_map = {}
    source_map = {}  # uid -> {"org": count, "event": count} for debug

    def fetch_punches(uid):
        org_url = f"{BASE}/api/v4/organizations/{ORG_ID}/users/{uid}/serviceTime"
        evt_url = f"{BASE}/api/v4/organizations/{ORG_ID}/events/{EVENT_ID}/users/{uid}/serviceTime"

        org_raw = safe_get_json(_auth_dict, org_url)
        evt_raw = safe_get_json(_auth_dict, evt_url)

        org_list = org_raw if isinstance(org_raw, list) else []
        evt_list = evt_raw if isinstance(evt_raw, list) else []

        # Merge on record id, preferring whichever has startTimestamp populated
        merged = {}
        for rec in org_list:
            rid = rec.get('id') or f"org-{rec.get('startTimestamp')}-{rec.get('eventShiftId')}"
            rec['_source'] = 'org'
            merged[rid] = rec
        for rec in evt_list:
            rid = rec.get('id') or f"evt-{rec.get('startTimestamp')}-{rec.get('eventShiftId')}"
            if rid in merged:
                # Prefer the version with more data
                existing = merged[rid]
                if not existing.get('startTimestamp') and rec.get('startTimestamp'):
                    rec['_source'] = 'event (preferred)'
                    merged[rid] = rec
                else:
                    existing['_source'] = existing.get('_source', 'org') + '+event'
            else:
                rec['_source'] = 'event'
                merged[rid] = rec

        return uid, list(merged.values()), {"org": len(org_list), "event": len(evt_list)}

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch_punches, uid) for uid in uids]
        for f in as_completed(futures):
            uid, plist, counts = f.result()
            punch_map[uid] = plist
            source_map[uid] = counts

    # 4. Clean up names
    final_roster = []
    for p in raw_people:
        fname, lname = p['fname'], p['lname']
        if not fname or fname.lower() in ("none", "volunteer"):
            fname = "Unknown"
            lname = f"(ID: {p['uid']})"
        p['fname'], p['lname'] = fname, lname
        final_roster.append(p)

    meta = {
        "sources": source_map,
        "presence": presence_data,
        "presence_error": presence_error,
    }
    return final_roster, punch_map, meta


# ─── App UI ───────────────────────────────────────────────────────────────────
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
                if st.session_state.auth_data:
                    st.rerun()
    else:
        st.success("Connected")
        if st.button("🔄 Refresh Now"):
            st.cache_data.clear()
            st.rerun()
        if st.button("Logout"):
            st.session_state.auth_data = None
            st.rerun()
        st.caption(f"Auto-refreshes every {REFRESH_SECS}s")

        st.divider()
        st.session_state.debug_mode = st.checkbox(
            "🔬 Debug mode",
            value=st.session_state.get("debug_mode", False),
            help="Shows raw serviceTime API responses for in-progress shifts so we can see what the API is actually returning."
        )

if st.session_state.auth_data:
    now = datetime.now(LOCAL_TZ)
    t_date = now.date()

    st.title(f"🐾 Refuge Roster — {t_date.strftime('%A, %b %d')}")

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

        roster, punches, meta = data
        sources = meta.get("sources", {})
        presence_list = meta.get("presence", [])
        presence_error = meta.get("presence_error")

        # Build two lookups from presence data:
        #   (uid, sid) -> latest status record  (shift-scoped clock-ins)
        #   uid -> latest status record         (event-level check-ins w/ sid=null)
        presence_by_shift = {}
        presence_by_user = {}
        for rec in presence_list:
            uid = rec.get("eventUserAccountId")
            sid = rec.get("eventShiftId")
            if uid is None:
                continue
            # Keep most recent record per key (by dateCreated string compare — ISO format)
            if sid:
                key = (uid, sid)
                prev = presence_by_shift.get(key)
                if not prev or (rec.get("dateCreated", "") > prev.get("dateCreated", "")):
                    presence_by_shift[key] = rec
            prev_u = presence_by_user.get(uid)
            if not prev_u or (rec.get("dateCreated", "") > prev_u.get("dateCreated", "")):
                presence_by_user[uid] = rec

    if roster:
        # ── Presence endpoint status banner ──
        if presence_error:
            st.warning(
                f"⚠️ `/presence` endpoint returned: **{presence_error}**. "
                "Live check-in status is unavailable — the dashboard will only show "
                "who has *completed* their shift (via serviceTime). It cannot show who "
                "is currently clocked in."
            )
        elif presence_list:
            st.success(
                f"✓ Live presence feed active — {len(presence_list)} presence records loaded."
            )
        # ── Debug panel (if enabled) — show raw serviceTime for users on in-progress shifts ──
        if st.session_state.get("debug_mode"):
            import json as _json

            st.markdown('<div class="section-header">🔬 Debug — /presence endpoint</div>',
                        unsafe_allow_html=True)
            if presence_error:
                st.error(f"GET /presence failed: `{presence_error}`")
            else:
                st.write(f"GET /presence returned **{len(presence_list)} records**")
                if presence_list:
                    with st.expander(f"View all {len(presence_list)} presence records"):
                        st.json(presence_list)

            in_progress = [v for v in roster
                           if v['start'] - timedelta(minutes=30) <= now <= v['end'] + timedelta(minutes=30)]

            st.markdown('<div class="section-header">🔬 Debug — Current / In-Progress Shifts</div>',
                        unsafe_allow_html=True)

            if not in_progress:
                st.info("No shifts currently in progress (±30 min window).")
            else:
                st.caption(
                    "For each user whose shift is currently active, this shows what "
                    "/serviceTime is returning AND what /presence says about them."
                )

                for v in in_progress:
                    user_p = punches.get(v['uid'], [])
                    # Only show records for today
                    todays = []
                    for p in user_p:
                        st_ts = p.get('startTimestamp')
                        if st_ts:
                            try:
                                dt = datetime.fromisoformat(st_ts.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                                if dt.date() == t_date:
                                    todays.append(p)
                            except Exception:
                                pass
                        elif not p.get('endTimestamp') and p.get('dayDate', '').startswith(t_date.isoformat()):
                            todays.append(p)

                    src = sources.get(v['uid'], {})
                    pres_rec = presence_by_shift.get((v['uid'], v['sid'])) or presence_by_user.get(v['uid'])
                    pres_label = f"presence: {pres_rec.get('status', '?')}" if pres_rec else "presence: none"

                    label = (f"{v['fname']} {v['lname']} · "
                             f"shift {v['start'].strftime('%I:%M %p')} (sid={v['sid']}, uid={v['uid']}) — "
                             f"svc org: {src.get('org', 0)}, svc evt: {src.get('event', 0)}, today: {len(todays)}, {pres_label}")

                    with st.expander(label):
                        if pres_rec:
                            st.write("**Presence record:**")
                            st.json(pres_rec)
                        if todays:
                            st.write("**ServiceTime records (today):**")
                            st.json(todays)
                        if not pres_rec and not todays:
                            st.warning(
                                "No presence record AND no serviceTime records for today. "
                                "If this user is actually clocked in, neither endpoint is exposing it "
                                "to this API token."
                            )

        # ── Assemble cards + status, collect counts ──
        cards = []
        counts = {"On Shift": 0, "Completed": 0, "Starting Soon": 0,
                  "Scheduled": 0, "No Show / Late": 0, "Late Out": 0}

        for v in roster:
            fullName = f"{v['fname']} {v['lname']}".strip()
            user_p = punches.get(v['uid'], [])
            my_punch = find_punch_for_shift(user_p, v, t_date)

            cin = (datetime.fromisoformat(my_punch['startTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                   if my_punch and my_punch.get('startTimestamp') else None)
            cout = (datetime.fromisoformat(my_punch['endTimestamp'].replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                    if my_punch and my_punch.get('endTimestamp') else None)

            # ── Consult presence endpoint for LIVE check-in state ──
            # presence.status values typically: "checkedIn", "clockedIn", "checkedOut",
            # "clockedOut" (exact strings may vary — we treat any non-"out" value as "in").
            presence_rec = presence_by_shift.get((v['uid'], v['sid'])) or presence_by_user.get(v['uid'])
            pres_status = (presence_rec or {}).get("status", "")
            pres_is_in = False
            if pres_status:
                s_lower = pres_status.lower()
                pres_is_in = ("in" in s_lower) and ("out" not in s_lower)

            p_str = (f"In: {cin.strftime('%I:%M %p') if cin else '--'}"
                     f" → Out: {cout.strftime('%I:%M %p') if cout else '--'}")
            if pres_is_in and not cin:
                # Live check-in, no serviceTime record yet — show presence timestamp
                pd = presence_rec.get("dateCreated", "")
                try:
                    dt = datetime.fromisoformat(pd.replace('Z', '+00:00')).astimezone(LOCAL_TZ)
                    p_str = f"In: {dt.strftime('%I:%M %p')} → Out: --  (live)"
                except Exception:
                    p_str = "In: (checked in) → Out: --"

            # Determine status
            if cin and cout:
                status, css = "Completed", "status-completed"
            elif cin or pres_is_in:
                if cin and now > v['end'] + timedelta(minutes=LATE_OUT_MINUTES):
                    status, css = "Late Out", "status-alert-red"
                else:
                    status, css = "On Shift", "status-checked-in"
            else:
                if now > v['start'] + timedelta(minutes=LATE_IN_MINUTES):
                    # Only alarm as No Show if presence endpoint IS working (we'd see them if they were in).
                    # If presence is unavailable, we genuinely don't know, so show a softer state.
                    if presence_error:
                        status, css = "In Progress (unknown)", "status-pending"
                    else:
                        status, css = "No Show / Late", "status-alert-red"
                elif now >= v['start'] - timedelta(minutes=60):
                    status, css = "Starting Soon", "status-upcoming"
                else:
                    status, css = "Scheduled", "status-pending"

            counts[status] = counts.get(status, 0) + 1

            cards.append({
                "time": v['start'],
                "status": status,
                "html": f"""
                <div class="shift-card {css}">
                    <div class="shift-time">{v['start'].strftime("%I:%M %p")} — {v['end'].strftime("%I:%M %p")}</div>
                    <div class="shift-name">{fullName}</div>
                    <div class="shift-role">{v['role']}</div>
                    <div class="punch-box">🕒 {p_str}</div>
                    <div style="margin-top:12px;"><span class="status-badge">{status}</span></div>
                </div>
                """,
            })

        # ── Summary meta bar ──
        total = len(cards)
        on_shift = counts.get("On Shift", 0)
        completed = counts.get("Completed", 0)
        upcoming = counts.get("Starting Soon", 0)
        scheduled = counts.get("Scheduled", 0)
        alerts = counts.get("No Show / Late", 0) + counts.get("Late Out", 0)

        st.markdown(f"""
        <div class="meta-bar">
            <div class="stat"><b>{total}</b> total shifts today</div>
            <div class="stat"><span class="dot dot-green"></span><b>{on_shift}</b> on shift</div>
            <div class="stat"><span class="dot dot-blue"></span><b>{upcoming}</b> starting soon</div>
            <div class="stat"><span class="dot dot-purple"></span><b>{completed}</b> completed</div>
            <div class="stat"><span class="dot dot-gray"></span><b>{scheduled}</b> scheduled</div>
            {'<div class="stat"><span class="dot dot-red"></span><b>' + str(alerts) + '</b> needs attention</div>' if alerts else ''}
            <div class="stat" style="margin-left:auto; color:#64748b;">Last sync: {now.strftime('%I:%M:%S %p')}</div>
        </div>
        """, unsafe_allow_html=True)

        # ── Render cards, sorted by time ──
        cards.sort(key=lambda x: x['time'])
        cols = st.columns(4)
        for i, card in enumerate(cards):
            with cols[i % 4]:
                st.markdown(card['html'], unsafe_allow_html=True)
    else:
        st.info(f"No volunteers scheduled for {t_date.strftime('%m/%d')}.")

    time.sleep(REFRESH_SECS)
    st.rerun()
else:
    st.info("Staff Login Required.")

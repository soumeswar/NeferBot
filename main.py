# app.py
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, Response, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime, timedelta
from dotenv import load_dotenv
from queue import Queue, Empty
import threading, subprocess, time, os, json
import traceback

# Optional psutil for metrics
try:
    import psutil
except Exception:
    psutil = None

load_dotenv()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
PORT = int(os.getenv("PORT", 10000))

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(minutes=60)

limiter = Limiter(get_remote_address, app=app, default_limits=["60/minute"])

# shared state
bot_proc = None              # subprocess.Popen object (or None)
bot_thread = None            # thread wrapper that monitors process
bot_log_queue = Queue()
bot_status = {"state": "stopped", "started_at": None, "pid": None, "last_exit_code": None}

# helper: push log
def push_log(s):
    ts = datetime.utcnow().isoformat() + "Z"
    bot_log_queue.put({"ts": ts, "line": str(s)})

# Bot runner that spawns bot.py and streams its stdout/stderr to queue
def bot_runner():
    global bot_proc, bot_status
    try:
        push_log("[SYS] bot_runner launched")
        script = os.path.join(os.path.dirname(__file__), "bot.py")
        # Use sys.executable to keep same python
        bot_proc = subprocess.Popen(
            [os.sys.executable, script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True
        )
        bot_status["state"] = "running"
        bot_status["started_at"] = time.time()
        bot_status["pid"] = bot_proc.pid
        bot_status["last_exit_code"] = None
        push_log(f"[SYS] bot started (pid={bot_proc.pid})")

        # stream output lines
        for line in bot_proc.stdout:
            push_log(line.rstrip())

        exit_code = bot_proc.wait()
        bot_status["state"] = "stopped"
        bot_status["last_exit_code"] = exit_code
        bot_status["pid"] = None
        push_log(f"[SYS] bot exited (code={exit_code})")

    except Exception as e:
        bot_status["state"] = "error"
        bot_status["pid"] = None
        bot_status["last_exit_code"] = None
        push_log(f"[SYS ERROR] {e}")
        push_log(traceback.format_exc())
    finally:
        bot_proc = None

# start/stop control helpers
def start_bot_background():
    global bot_thread
    if bot_thread and bot_thread.is_alive():
        return False
    bot_thread = threading.Thread(target=bot_runner, daemon=True)
    bot_thread.start()
    return True

def stop_bot_graceful(timeout=5):
    global bot_proc
    if bot_proc and bot_proc.poll() is None:
        try:
            push_log("[SYS] Terminating bot (SIGTERM)")
            bot_proc.terminate()
            try:
                bot_proc.wait(timeout=timeout)
                push_log("[SYS] Bot terminated gracefully")
            except subprocess.TimeoutExpired:
                push_log("[SYS] Terminate timed out — killing (SIGKILL)")
                bot_proc.kill()
                bot_proc.wait()
        except Exception as e:
            push_log("[SYS ERROR] while stopping: " + str(e))
    else:
        push_log("[SYS] stop requested but bot is not running")

# Simple auth
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper

# Routes

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login():
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == ADMIN_PASSWORD:
            session["logged_in"] = True
            session.permanent = True
            flash("Welcome, admin!", "success")
            nxt = request.args.get("next") or url_for("admin")
            return redirect(nxt)
        else:
            flash("Wrong password", "error")
    return render_template_string(LOGIN_HTML)

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out", "info")
    return redirect(url_for("login"))

# Public root: simple bot status page (no login)
@app.route("/")
def index():
    state = bot_status.get("state", "stopped")
    pid = bot_status.get("pid")
    started_at = bot_status.get("started_at")
    uptime = ""
    if started_at:
        uptime = int(time.time() - started_at)
    return render_template_string(PUBLIC_INDEX_HTML, state=state, pid=pid, uptime=uptime)

# Admin dashboard (protected)
@app.route("/admin")
@login_required
def admin():
    return render_template_string(ADMIN_HTML, psutil_available=(psutil is not None))

# Control endpoints (POST to avoid CSRF via simple forms)
@app.route("/admin/start", methods=["POST"])
@login_required
def admin_start():
    if bot_status.get("state") == "running":
        flash("Bot is already running", "info")
    else:
        started = start_bot_background()
        flash("Start requested" if started else "Start failed", "success")
    return redirect(url_for("admin"))

@app.route("/admin/stop", methods=["POST"])
@login_required
def admin_stop():
    if bot_status.get("state") != "running":
        flash("Bot not running", "info")
    else:
        stop_bot_graceful()
        flash("Stop requested", "success")
    return redirect(url_for("admin"))

@app.route("/admin/restart", methods=["POST"])
@login_required
def admin_restart():
    # stop then start
    stop_bot_graceful()
    time.sleep(0.5)
    start_bot_background()
    flash("Restart requested", "success")
    return redirect(url_for("admin"))

# SSE stream endpoint for logs + status + metrics
def stream_generator():
    last_sent = 0
    # Keep running until client disconnects
    while True:
        try:
            # send pending logs
            try:
                while True:
                    item = bot_log_queue.get_nowait()
                    payload = json.dumps({"type":"log", "payload": item})
                    yield f"event: log\ndata: {payload}\n\n"
            except Empty:
                pass

            # periodic status update every 1s
            s = {
                "type": "status",
                "payload": {
                    "state": bot_status.get("state"),
                    "pid": bot_status.get("pid"),
                    "started_at": bot_status.get("started_at"),
                    "last_exit_code": bot_status.get("last_exit_code"),
                    "uptime": int(time.time() - bot_status.get("started_at")) if bot_status.get("started_at") else 0
                }
            }
            yield f"event: status\ndata: {json.dumps(s)}\n\n"

            # metrics (cpu/mem)
            if psutil:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory().percent
                metrics = {"type":"metrics","payload":{"cpu":cpu,"mem":mem}}
            else:
                metrics = {"type":"metrics","payload":{"cpu":None,"mem":None}}
            yield f"event: metrics\ndata: {json.dumps(metrics)}\n\n"

            time.sleep(1)
        except GeneratorExit:
            break
        except Exception:
            push_log("[SYS ERROR] sse stream error")
            time.sleep(1)

@app.route("/stream")
@login_required
def stream():
    return Response(stream_generator(), mimetype="text/event-stream")

# small API for recent logs (fallback)
@app.route("/api/logs")
@login_required
def api_logs():
    # Return last 200 items from queue snapshot (non-destructive)
    items = list(bot_log_queue.queue)[-200:]
    return jsonify(items)

# HTML templates (kept inline for single-file convenience)
LOGIN_HTML = """
<!doctype html>
<title>Login — NeferBot Admin</title>
<style>
body{background:#05060a;color:#dfffea;font-family:Inter,ui-sans-serif;padding:40px}
.container{max-width:420px;margin:60px auto;background:rgba(255,255,255,0.03);padding:28px;border-radius:12px}
input{width:100%;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:#fff}
button{margin-top:12px;padding:10px 14px;border-radius:8px;border:none;background:#00ffaa;color:#000;font-weight:600}
.flash{color:#ff9b9b;margin-top:10px}
</style>
<div class="container">
  <h2>🔒 Admin Login</h2>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in messages %}<div class="flash">{{msg}}</div>{% endfor %}
  {% endwith %}
  <form method="post">
    <input name="password" type="password" placeholder="Admin password" autofocus required>
    <button type="submit">Login</button>
  </form>
</div>
"""

PUBLIC_INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>NeferBot — Public</title>
<style>
body{font-family:Inter,system-ui,Segoe UI,Roboto;padding:40px;background:#030305;color:#dfffea}
.card{background:rgba(255,255,255,0.03);padding:20px;border-radius:12px;max-width:800px;margin:auto}
.btn{display:inline-block;padding:10px 14px;border-radius:8px;background:#00ffaa;color:#000;text-decoration:none;font-weight:700}
.small{color:#9ef0d6}
</style>
</head>
<body>
<div class="card">
  <h1>NeferBot</h1>
  <p class="small">Public bot page — status below.</p>
  <p><strong>Status:</strong> {{ state }} {% if pid %} (pid {{pid}}) {% endif %}</p>
  <p><strong>Uptime (s):</strong> {{ uptime }}</p>
  <p><a class="btn" href="/admin">Admin dashboard</a></p>
</div>
</body>
</html>
"""

ADMIN_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>NeferBot Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#05060a; --panel: rgba(255,255,255,0.03); --accent:#00ffaa; --muted:#93f5d6;
}
*{box-sizing:border-box}
body{font-family:Inter,system-ui,Arial;background:linear-gradient(180deg,#03040a,#07090f);color:var(--muted);margin:0;padding:30px}
.wrap{max-width:1100px;margin:0 auto}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.brand h1{color:var(--accent);margin:0}
.controls{display:flex;gap:10px;align-items:center}
.btn{background:var(--accent);color:#000;border:none;padding:10px 14px;border-radius:10px;font-weight:700;cursor:pointer}
.ghost{background:transparent;color:var(--accent);border:1px solid rgba(0,255,170,0.12)}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:18px}
.panel{background:var(--panel);padding:18px;border-radius:12px;border:1px solid rgba(0,255,170,0.06);box-shadow:0 6px 30px rgba(0,255,170,0.02)}
.row{display:flex;gap:12px;align-items:center;justify-content:space-between}
.status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}
.status-running{background:#3ef39c}
.status-stopped{background:#ff8b8b}
.metrics{display:flex;gap:12px;margin-top:12px}
.metric{flex:1;background:rgba(255,255,255,0.02);padding:12px;border-radius:10px;text-align:center}
.logbox{height:360px;overflow:auto;background:#02040a;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.03);font-family:monospace;color:#dfffea}
.log-line{padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.01);font-size:13px}
.controls small{color:#9ef0d6;display:block;text-align:right}
.switch {display:inline-flex;align-items:center;gap:8px}
.theme-toggle{padding:8px;border-radius:8px;background:transparent;border:1px solid rgba(255,255,255,0.03);color:var(--muted);cursor:pointer}
.spark{height:28px;width:100%;background:linear-gradient(90deg,#083,#0fa);border-radius:6px}
.footer{margin-top:18px;color:#8ff0d0;font-size:13px}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="brand">
      <h1>⚙️ NeferBot Admin</h1>
      <div style="font-size:13px;color:#9ef0d6">Secure admin panel</div>
    </div>
    <div class="controls">
      <form method="post" action="/admin/start" style="display:inline"><button class="btn">Start</button></form>
      <form method="post" action="/admin/stop" style="display:inline"><button class="btn ghost">Stop</button></form>
      <form method="post" action="/admin/restart" style="display:inline"><button class="btn">Restart</button></form>
      <a href="/logout" class="theme-toggle" style="margin-left:6px">Logout</a>
    </div>
  </div>

  <div class="grid">
    <div class="panel">
      <div class="row">
        <div>
          <div id="statusRow"><span id="statusDot" class="status-dot status-stopped"></span><strong id="statusText">Stopped</strong></div>
          <div style="font-size:13px;color:#93f5d6" id="uptime">Uptime: 0s</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:13px;color:#9ef0d6">PID: <span id="pid">—</span></div>
          <div style="font-size:12px;color:#7fe6c4" id="exitcode">Last exit: —</div>
        </div>
      </div>

      <div class="metrics" style="margin-top:18px">
        <div class="metric">
          <div style="font-size:12px;color:#9ef0d6">CPU</div>
          <div style="font-size:18px" id="cpuVal">—%</div>
          <div class="spark" id="cpuSpark"></div>
        </div>
        <div class="metric">
          <div style="font-size:12px;color:#9ef0d6">MEM</div>
          <div style="font-size:18px" id="memVal">—%</div>
          <div class="spark" id="memSpark"></div>
        </div>
        <div class="metric">
          <div style="font-size:12px;color:#9ef0d6">Logs (live)</div>
          <div style="font-size:18px" id="logCount">0</div>
          <div style="height:6px"></div>
        </div>
      </div>

      <h3 style="margin-top:16px">Live Logs</h3>
      <div id="logbox" class="logbox"></div>
    </div>

    <div class="panel">
      <h3>Quick Info</h3>
      <p style="color:#9ef0d6;font-size:13px">psutil available: {{ 'yes' if psutil_available else 'no (install psutil)' }}</p>
      <p style="font-size:13px">Last 200 log lines are visible. Use Start/Stop/Restart buttons above to control the bot.</p>

      <h4 style="margin-top:12px">Activity (simple)</h4>
      <div style="height:80px;margin-top:8px">
        <!-- tiny pure-CSS activity (just decorative) -->
        <svg width="100%" height="80" viewBox="0 0 200 80" preserveAspectRatio="none">
          <polyline id="activityLine" points="0,50 30,40 60,55 90,30 120,45 150,20 180,50 200,35" fill="none" stroke="#00ffaa" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity="0.18"/>
        </svg>
      </div>

      <div style="margin-top:12px">
        <button class="btn" onclick="downloadLogs()">Download logs</button>
      </div>
    </div>
  </div>

  <div class="footer">NeferBot v2 · Live dashboard · Server time: <span id="now">—</span></div>
</div>

<script>
let evtSource = null;
let logbox = document.getElementById("logbox");
let logCount = 0;
function addLog(obj){
  let el = document.createElement("div");
  el.className = "log-line";
  el.textContent = obj.ts + "  " + obj.line;
  logbox.appendChild(el);
  logCount++;
  document.getElementById("logCount").textContent = logCount;
  logbox.scrollTop = logbox.scrollHeight;
}

function ensureConnection(){
  if (evtSource) return;
  evtSource = new EventSource("/stream");
  evtSource.addEventListener("log", function(e){
    try{
      const data = JSON.parse(e.data);
      addLog(data.payload);
    }catch(err){}
  });
  evtSource.addEventListener("status", function(e){
    try{
      const data = JSON.parse(e.data);
      const st = data.payload;
      document.getElementById("statusText").textContent = st.state || "unknown";
      document.getElementById("pid").textContent = st.pid || "—";
      document.getElementById("uptime").textContent = "Uptime: " + (st.uptime || 0) + "s";
      document.getElementById("exitcode").textContent = "Last exit: " + (st.last_exit_code === null ? "—" : st.last_exit_code);
      const dot = document.getElementById("statusDot");
      dot.className = "status-dot " + (st.state==="running" ? "status-running" : "status-stopped");
    }catch(err){}
  });
  evtSource.addEventListener("metrics", function(e){
    try{
      const data = JSON.parse(e.data).payload;
      document.getElementById("cpuVal").textContent = (data.cpu===null ? "N/A" : data.cpu.toFixed(0)+"%")
      document.getElementById("memVal").textContent = (data.mem===null ? "N/A" : data.mem.toFixed(0)+"%")
      // tiny sparkline color fill
      document.getElementById("cpuSpark").style.width = (data.cpu? Math.min(100,data.cpu) : 0)+"%";
      document.getElementById("memSpark").style.width = (data.mem? Math.min(100,data.mem) : 0)+"%";
    }catch(err){}
  });
  evtSource.onerror = function(){ /* will reconnect automatically */ }
}

ensureConnection();

function downloadLogs(){
  fetch("/api/logs").then(r=>r.json()).then(arr=>{
    const text = arr.map(x=> (x.ts + " " + x.line)).join("\\n");
    const b = new Blob([text], {type:'text/plain'});
    const url = URL.createObjectURL(b);
    const a = document.createElement("a");
    a.href = url; a.download = "neferbot_logs.txt"; document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  });
}

// show server time
setInterval(()=>document.getElementById("now").textContent = new Date().toLocaleString(),1000);
</script>
</body>
</html>
"""

# Run the app
if __name__ == "__main__":
    # By default DO NOT auto-start the bot on server start.
    # If you want it to start automatically, uncomment the line below:
    # start_bot_background()
    app.run(host="0.0.0.0", port=PORT, debug=False)

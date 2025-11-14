#!/usr/bin/env python3
# app.py — NeferBot Admin Dashboard V3 (Neon UI)
# Usage: python app.py
# Requires: flask, flask_limiter, python-dotenv, (optional) psutil

from flask import Flask, render_template_string, request, redirect, url_for, session, flash, Response, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from datetime import datetime, timedelta
from dotenv import load_dotenv
from queue import Queue, Empty
import threading, subprocess, time, os, json, sys
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
AUTO_START = os.getenv("AUTO_START", "false").lower() in ("1", "true", "yes")

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(minutes=60)

limiter = Limiter(get_remote_address, app=app, default_limits=["60/minute"])

# Shared state
bot_proc = None              # subprocess.Popen object (or None)
bot_thread = None            # thread wrapper that monitors process
bot_log_queue = Queue()
bot_status = {"state": "stopped", "started_at": None, "pid": None, "last_exit_code": None}
admin_cmd_queue = Queue(maxsize=200)

# Helper: push log
def push_log(s):
    ts = datetime.utcnow().isoformat() + "Z"
    item = {"ts": ts, "line": str(s)}
    try:
        bot_log_queue.put_nowait(item)
    except:
        # If queue full, discard oldest and push
        try:
            _ = bot_log_queue.get_nowait()
        except:
            pass
        try:
            bot_log_queue.put_nowait(item)
        except:
            pass

# Bot runner that spawns bot.py and streams stdout/stderr to queue
def bot_runner():
    global bot_proc, bot_status
    try:
        push_log("[SYS] bot_runner launched")
        script = os.path.join(os.path.dirname(__file__), "bot.py")
        if not os.path.exists(script):
            push_log(f"[SYS ERROR] bot.py not found at {script}")
            bot_status["state"] = "stopped"
            return

        # Use same Python interpreter as this process
        cmd = [sys.executable, script]
        # Start process
        bot_proc = subprocess.Popen(
            cmd,
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

        # Stream output
        try:
            for line in bot_proc.stdout:
                if line is None:
                    break
                push_log(line.rstrip())
        except Exception as e:
            push_log(f"[SYS ERROR] Exception while reading bot stdout: {e}")
            push_log(traceback.format_exc())

        # Wait for exit
        try:
            exit_code = bot_proc.wait()
        except Exception:
            exit_code = None
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

# Start/stop control helpers
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

@app.route("/")
def index():
    state = bot_status.get("state", "stopped")
    pid = bot_status.get("pid")
    started_at = bot_status.get("started_at")
    uptime = ""
    if started_at:
        uptime = int(time.time() - started_at)
    return render_template_string(PUBLIC_INDEX_HTML, state=state, pid=pid, uptime=uptime)

@app.route("/admin")
@login_required
def admin():
    return render_template_string(ADMIN_HTML, psutil_available=(psutil is not None))

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
    stop_bot_graceful()
    time.sleep(0.5)
    start_bot_background()
    flash("Restart requested", "success")
    return redirect(url_for("admin"))

# Optional admin command endpoint (queues simple text commands)
@app.route("/admin/cmd", methods=["POST"])
@login_required
def admin_cmd():
    try:
        cmd = request.form.get("cmd", "").strip()
        if not cmd:
            flash("Empty command", "error")
        else:
            try:
                admin_cmd_queue.put_nowait({"ts": time.time(), "cmd": cmd})
                push_log(f"[SYS] Admin command queued: {cmd}")
                flash("Command queued", "success")
            except Exception:
                flash("Command queue full", "error")
    except Exception as e:
        flash("Failed to queue command", "error")
        push_log(f"[SYS ERROR] admin_cmd endpoint error: {e}")
    return redirect(url_for("admin"))

# SSE stream endpoint for logs + status + metrics
def stream_generator():
    # We will stream logs, status, metrics.
    try:
        while True:
            # Send pending logs
            try:
                while True:
                    item = bot_log_queue.get_nowait()
                    payload = json.dumps({"type":"log", "payload": item})
                    yield f"event: log\ndata: {payload}\n\n"
            except Empty:
                pass

            # Periodic status update
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

            # Metrics
            if psutil:
                try:
                    cpu = psutil.cpu_percent(interval=None)
                    mem = psutil.virtual_memory().percent
                    metrics = {"type":"metrics","payload":{"cpu":cpu,"mem":mem}}
                except Exception:
                    metrics = {"type":"metrics","payload":{"cpu":None,"mem":None}}
            else:
                metrics = {"type":"metrics","payload":{"cpu":None,"mem":None}}
            yield f"event: metrics\ndata: {json.dumps(metrics)}\n\n"

            # Admin command drain (informational)
            try:
                while True:
                    cmd = admin_cmd_queue.get_nowait()
                    payload = json.dumps({"type":"admin_cmd", "payload":cmd})
                    yield f"event: admin_cmd\ndata: {payload}\n\n"
            except Empty:
                pass

            time.sleep(1)
    except GeneratorExit:
        return
    except Exception as e:
        push_log("[SYS ERROR] sse stream error: " + str(e))
        time.sleep(1)
        return

@app.route("/stream")
@login_required
def stream():
    return Response(stream_generator(), mimetype="text/event-stream")

@app.route("/api/logs")
@login_required
def api_logs():
    items = list(bot_log_queue.queue)[-200:]
    return jsonify(items)

# HTML templates (V3 neon UI)
LOGIN_HTML = """
<!doctype html>
<title>Login — NeferBot Admin</title>
<style>
/* Minimal clean login styling */
body{background:#06060b;color:#c9fff0;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial;padding:40px}
.container{max-width:420px;margin:80px auto;background:linear-gradient(180deg,rgba(255,255,255,0.02),rgba(255,255,255,0.01));padding:28px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,0.6)}
input{width:100%;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.04);background:transparent;color:#fff;margin-top:10px}
button{margin-top:12px;padding:10px 14px;border-radius:8px;border:none;background:#00ffd6;color:#002;cursor:pointer;font-weight:700}
.flash{color:#ff9b9b;margin-top:10px}
h2{margin:0 0 8px 0}
small{color:#9ef0d6}
</style>
<div class="container">
  <h2>🔐 NeferBot Admin</h2>
  <small>Enter admin password to continue</small>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in messages %}<div class="flash">{{msg}}</div>{% endfor %}
  {% endwith %}
  <form method="post">
    <input name="password" type="password" placeholder="Admin password" autofocus required>
    <button type="submit">Unlock</button>
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
body{font-family:Inter,system-ui,Segoe UI,Roboto;padding:40px;background:linear-gradient(180deg,#03040a,#06131a);color:#bfffe8}
.card{background:rgba(255,255,255,0.02);padding:20px;border-radius:12px;max-width:900px;margin:auto;box-shadow:0 6px 30px rgba(0,255,170,0.02)}
.btn{display:inline-block;padding:10px 14px;border-radius:8px;background:#00ffd6;color:#001;font-weight:800;text-decoration:none}
.small{color:#9ef0d6}
</style>
</head>
<body>
<div class="card">
  <h1>NeferBot</h1>
  <p class="small">Public bot status</p>
  <p><strong>Status:</strong> {{ state }} {% if pid %} (pid {{pid}}) {% endif %}</p>
  <p><strong>Uptime (s):</strong> {{ uptime }}</p>
  <p><a class="btn" href="/admin">Open Admin Dashboard</a></p>
</div>
</body>
</html>
"""

ADMIN_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>NeferBot Admin v3 — Neon</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;500;700;900&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#03040a; --panel:rgba(255,255,255,0.03); --accent:#00ffd6; --muted:#9ef0d6; --danger:#ff7b7b;
}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,Arial;background:linear-gradient(180deg,#02030a,#041018);color:var(--muted);min-height:100vh}
.container{max-width:1200px;margin:28px auto;padding:20px}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.brand h1{margin:0;color:var(--accent);letter-spacing:0.4px}
.controls{display:flex;gap:8px;align-items:center}
.btn{background:var(--accent);color:#002;border:none;padding:10px 14px;border-radius:10px;font-weight:800;cursor:pointer}
.btn.ghost{background:transparent;border:1px solid rgba(0,255,214,0.06);color:var(--muted)}
.panel-grid{display:grid;grid-template-columns:2fr 1fr;gap:18px}
.panel{background:var(--panel);padding:18px;border-radius:12px;border:1px solid rgba(0,255,170,0.04);box-shadow:0 6px 40px rgba(0,0,0,0.6)}
.row{display:flex;align-items:center;justify-content:space-between}
.status{display:flex;align-items:center;gap:12px}
.dot{width:12px;height:12px;border-radius:50%;background:#ff7b7b;box-shadow:0 0 6px rgba(255,120,120,0.12)}
.dot.running{background:#7bffda;box-shadow:0 0 14px rgba(0,255,214,0.12)}
.metrics{display:flex;gap:12px;margin-top:12px}
.metric{flex:1;background:rgba(255,255,255,0.02);padding:12px;border-radius:10px;text-align:center}
.metric .val{font-size:18px;font-weight:800;color:#eafff6}
.logbox{height:420px;overflow:auto;background:#02040a;padding:12px;border-radius:8px;border:1px solid rgba(255,255,255,0.02);font-family:monospace;color:#bfffe8}
.log-line{padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.01);font-size:13px}
.controls .small{font-size:12px;color:#9ef0d6}
.footer{margin-top:18px;color:#8ff0d0;font-size:13px}
.form-row{display:flex;gap:8px;margin-top:8px}
.input{flex:1;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.03);background:transparent;color:var(--muted)}
.cmd-btn{padding:8px 10px;border-radius:8px;border:none;background:#00444a;color:#c7fff3;font-weight:700}
.badge{display:inline-block;padding:6px 10px;border-radius:999px;background:rgba(0,255,214,0.06);font-weight:700}
.small-muted{font-size:12px;color:#93f5d6}
.download-btn{margin-top:10px;background:transparent;border:1px solid rgba(0,255,214,0.06);padding:8px;border-radius:8px;color:var(--muted)}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="brand">
      <h1>⚙️ NeferBot Admin v3</h1>
      <div class="small-muted">Secure, Neon control panel</div>
    </div>
    <div class="controls">
      <form method="post" action="/admin/start" style="display:inline"><button class="btn">Start</button></form>
      <form method="post" action="/admin/stop" style="display:inline"><button class="btn ghost">Stop</button></form>
      <form method="post" action="/admin/restart" style="display:inline"><button class="btn">Restart</button></form>
      <a href="/logout" class="btn ghost">Logout</a>
    </div>
  </div>

  <div class="panel-grid">
    <div class="panel">
      <div class="row" style="align-items:flex-start">
        <div>
          <div class="status">
            <div id="statusDot" class="dot"></div>
            <div>
              <div id="statusText" style="font-weight:800;font-size:20px">Stopped</div>
              <div class="small-muted" id="uptime">Uptime: 0s</div>
            </div>
          </div>
        </div>
        <div style="text-align:right">
          <div class="small-muted">PID: <span id="pid">—</span></div>
          <div class="small-muted">Last exit: <span id="exitcode">—</span></div>
        </div>
      </div>

      <div class="metrics">
        <div class="metric">
          <div class="small-muted">CPU</div>
          <div class="val" id="cpuVal">—%</div>
        </div>
        <div class="metric">
          <div class="small-muted">MEM</div>
          <div class="val" id="memVal">—%</div>
        </div>
        <div class="metric">
          <div class="small-muted">Live Logs</div>
          <div class="val" id="logCount">0</div>
        </div>
      </div>

      <h3 style="margin-top:16px">Live Logs</h3>
      <div id="logbox" class="logbox"></div>

      <div style="margin-top:12px">
        <button class="download-btn" onclick="downloadLogs()">Download last logs</button>
      </div>
    </div>

    <div class="panel">
      <h3>Quick Controls</h3>
      <p class="small-muted">psutil available: {{ 'yes' if psutil_available else 'no (install psutil)' }}</p>

      <form method="post" action="/admin/cmd">
        <div class="form-row">
          <input name="cmd" class="input" placeholder="Queue admin command (info only)">
          <button class="cmd-btn">Queue</button>
        </div>
      </form>

      <h4 style="margin-top:14px">Recent Activity</h4>
      <div id="recent" class="small-muted">No activity yet</div>

      <h4 style="margin-top:14px">About</h4>
      <p class="small-muted">NeferBot v3 — Neon dashboard. Use Start/Stop/Restart to control your bot process.</p>
    </div>
  </div>

  <div class="footer">Server time: <span id="now">—</span></div>
</div>

<script>
let evtSource = null;
let logbox = document.getElementById("logbox");
let logCount = 0;
let recentEl = document.getElementById("recent");

function addLog(obj){
  let el = document.createElement("div");
  el.className = "log-line";
  el.textContent = obj.ts + "  " + obj.line;
  logbox.appendChild(el);
  logCount++;
  document.getElementById("logCount").textContent = logCount;
  logbox.scrollTop = logbox.scrollHeight;
  // update recent preview
  try {
    const line = obj.line || "";
    if (line.length > 0) {
      recentEl.textContent = line.slice(0, 200);
    }
  } catch(e){}
}

function ensureConnection(){
  if (evtSource) return;
  evtSource = new EventSource("/stream");

  evtSource.addEventListener("log", function(e){
    try {
      const data = JSON.parse(e.data);
      addLog(data.payload);
    } catch(err){}
  });

  evtSource.addEventListener("status", function(e){
    try {
      const data = JSON.parse(e.data);
      const st = data.payload;
      const dot = document.getElementById("statusDot");
      const text = document.getElementById("statusText");
      text.textContent = (st.state || "unknown").toString().replace(/(^|\s)\S/g, l => l.toUpperCase());
      if (st.state === "running") {
        dot.classList.add("running");
      } else {
        dot.classList.remove("running");
      }
      document.getElementById("pid").textContent = st.pid || "—";
      document.getElementById("exitcode").textContent = st.last_exit_code !== null ? st.last_exit_code : "—";
      document.getElementById("uptime").textContent = "Uptime: " + (st.uptime || 0) + "s";
    } catch(err){}
  });

  evtSource.addEventListener("metrics", function(e){
    try {
      const data = JSON.parse(e.data);
      const m = data.payload;
      document.getElementById("cpuVal").textContent = m.cpu !== null ? m.cpu.toFixed(0) + "%" : "—";
      document.getElementById("memVal").textContent = m.mem !== null ? m.mem.toFixed(0) + "%" : "—";
    } catch(err){}
  });

  evtSource.addEventListener("admin_cmd", function(e){
    try {
      const data = JSON.parse(e.data);
      // optional: show last admin cmd
      const payload = data.payload;
      if (payload && payload.cmd) {
        recentEl.textContent = "Last cmd: " + payload.cmd;
      }
    } catch(err){}
  });

  evtSource.onerror = function(){
    // close and reconnect
    try { evtSource.close(); } catch(e){}
    evtSource = null;
    setTimeout(ensureConnection, 1200);
  };
}

function downloadLogs(){
  fetch("/api/logs").then(r=>r.json()).then(arr=>{
    const text = arr.map(x => x.ts + "  " + x.line).join("\\n");
    const blob = new Blob([text], {type: "text/plain"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "neferbot_logs.txt";
    a.click();
    URL.revokeObjectURL(url);
  });
}

setInterval(()=> {
  document.getElementById("now").textContent = new Date().toLocaleString();
}, 1000);

ensureConnection();
</script>
</body>
</html>
"""

# Run the app
if __name__ == "__main__":
    if AUTO_START:
        try:
            start_bot_background()
        except Exception as e:
            push_log(f"[SYS ERROR] auto-start failed: {e}")
    app.run(host="0.0.0.0", port=PORT, debug=False)

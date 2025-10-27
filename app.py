# app.py
from flask import Flask, render_template, request, redirect, url_for, session, g, flash, jsonify
import sqlite3
import os
import secrets
from openai import OpenAI
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

load_dotenv()
client = None
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception:
    client = None

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(16))
DATABASE = "events.db"

# Upload settings
UPLOAD_FOLDER = os.path.join("static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "avif"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------- Database helpers ----------
def get_db():
    if "_database" not in g:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        g._database = conn
    return g._database


@app.teardown_appcontext
def close_connection(exception):
    db = g.pop("_database", None)
    if db is not None:
        db.close()


def ensure_events_created_by_column():
    db = get_db()
    cur = db.cursor()
    cur.execute("PRAGMA table_info(events)")
    cols = [r["name"] for r in cur.fetchall()]
    if "created_by" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN created_by TEXT DEFAULT 'admin'")
        db.commit()


def init_db():
    db = get_db()
    cursor = db.cursor()
    cursor.executescript(
        """
    CREATE TABLE IF NOT EXISTS organizers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        date TEXT NOT NULL,
        venue TEXT NOT NULL,
        description TEXT,
        photo TEXT
    );

    CREATE TABLE IF NOT EXISTS participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        event_id INTEGER,
        FOREIGN KEY (event_id) REFERENCES events(id)
    );

    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        comment TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (event_id) REFERENCES events(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS cohosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(id)
);
    """
    )
    db.commit()
    ensure_events_created_by_column()
    print("✅ Database initialized or migrated.")


# ---------- Home ----------
@app.route("/")
def index():
    db = get_db()
    events = db.execute("SELECT * FROM events ORDER BY date ASC").fetchall()
    return render_template(
    "index.html",
    events=events,
    is_org="org" in session,
    user=session.get("user"),
    datetime=datetime
)

# ==============================
# 🔹 ORGANIZER ADMIN SECTION 🔹
# ==============================
@app.route("/admin")
def admin():
    if "org" not in session:
        return redirect(url_for("admin_login"))

    db = get_db()
    db.row_factory = sqlite3.Row  # ✅ ensures rows behave like dicts
    events = db.execute("SELECT * FROM events ORDER BY date ASC").fetchall()
    comments = db.execute("SELECT * FROM comments").fetchall()
    cohosts_data = {}
    for e in events:
        cohosts_data[e["id"]] = db.execute("SELECT * FROM cohosts WHERE event_id=?", (e["id"],)).fetchall()

    db.close()
    
    comments_list = []
    for c in comments:
        c = dict(c)
        ts = c.get("timestamp")
        if isinstance(ts, str):
            try:
                c["timestamp"] = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    c["timestamp"] = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
                except:
                    pass
        comments_list.append(c)

    return render_template(
        "admin.html",
        events=events,
        comments=comments_list,
        is_org=True,
        datetime=datetime
    )
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    db = get_db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        org = db.execute("SELECT * FROM organizers WHERE username=?", (username,)).fetchone()
        if org and check_password_hash(org["password"], password):
            session["org"] = username
            flash("Admin login successful!", "success")
            return redirect(url_for("admin"))
        flash("Invalid credentials", "danger")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("org", None)
    flash("Admin logged out.", "info")
    return redirect(url_for("index"))


@app.route("/admin/add", methods=["POST"])
def add_event():
    if "org" not in session:
        return redirect(url_for("admin_login"))
    name = request.form.get("name", "").strip()
    date = request.form.get("date", "").strip()
    venue = request.form.get("venue", "").strip()
    description = request.form.get("description", "").strip()
    photo = None
    if "photo" in request.files:
        file = request.files["photo"]
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            photo = filename
    db = get_db()
    db.execute(
        "INSERT INTO events (name, date, venue, description, photo, created_by) VALUES (?, ?, ?, ?, ?, ?)",
        (name, date, venue, description, photo, "admin"),
    )
    db.commit()
    flash("Event added successfully!", "success")
    return redirect(url_for("admin"))


@app.route("/admin/update", methods=["POST"])
def update_event():
    if "org" not in session:
        return redirect(url_for("admin_login"))
    db = get_db()
    id_ = request.form.get("id")
    name = request.form.get("name", "").strip()
    date = request.form.get("date", "").strip()
    venue = request.form.get("venue", "").strip()
    description = request.form.get("description", "")
    photo_filename = request.form.get("current_photo")
    if "photo" in request.files:
        file = request.files["photo"]
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            photo_filename = filename
    db.execute(
        "UPDATE events SET name=?, date=?, venue=?, description=?, photo=? WHERE id=?",
        (name, date, venue, description, photo_filename, id_),
    )
    db.commit()
    flash("Event updated successfully!", "success")
    return redirect(url_for("admin"))


@app.route("/admin/delete/<int:id>")
def delete_event(id):
    if "org" not in session:
        return redirect(url_for("admin_login"))
    db = get_db()
    db.execute("DELETE FROM events WHERE id=?", (id,))
    db.commit()
    flash("Event deleted successfully!", "info")
    return redirect(url_for("admin"))


@app.route("/admin/delete_comment/<int:comment_id>")
def delete_comment(comment_id):
    if "org" not in session:
        return redirect(url_for("admin_login"))
    db = get_db()
    db.execute("DELETE FROM comments WHERE id=?", (comment_id,))
    db.commit()
    flash("Comment deleted successfully.", "info")
    return redirect(request.referrer or url_for("admin"))


@app.route("/admin/users")
def admin_users():
    if "org" not in session:
        return redirect(url_for("admin_login"))
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY username ASC").fetchall()
    return render_template("admin_users.html", users=users, is_org=True)


# ==============================
# 🔹 USER SECTION 🔹
# ==============================
@app.route("/user/signup", methods=["GET", "POST"])
def user_signup():
    db = get_db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not username or not email or not password:
            flash("Please fill all fields", "warning")
            return redirect(url_for("user_signup"))
        cur = db.cursor()
        cur.execute("SELECT * FROM users WHERE email=?", (email,))
        if cur.fetchone():
            flash("Email already registered!", "danger")
            return redirect(url_for("user_signup"))
        hashed = generate_password_hash(password)
        cur.execute("INSERT INTO users (username, email, password) VALUES (?, ?, ?)", (username, email, hashed))
        db.commit()
        flash("Account created successfully! Please login.", "success")
        return redirect(url_for("user_login"))
    return render_template("user_signup.html")


@app.route("/user/login", methods=["GET", "POST"])
def user_login():
    db = get_db()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password"], password):
            session["user"] = {"id": user["id"], "username": user["username"], "email": user["email"]}
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("user_dashboard"))
        flash("Invalid email or password!", "danger")
    return render_template("user_login.html")


@app.route("/user/logout")
def user_logout():
    session.pop("user", None)
    flash("Logged out successfully.", "info")
    return redirect(url_for("index"))


@app.route("/user/dashboard")
def user_dashboard():
    if "user" not in session:
        flash("Please log in first", "warning")
        return redirect(url_for("user_login"))
    db = get_db()
    user_email = session["user"]["email"]
    rows = db.execute("SELECT * FROM events WHERE created_by=?", (user_email,)).fetchall()
    return render_template("user_dashboard.html", user=session.get("user"), my_events=rows)


@app.route("/user/add-event", methods=["GET", "POST"])
def user_add_event():
    if "user" not in session:
        flash("Please log in to post an event.", "warning")
        return redirect(url_for("user_login"))
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        date = request.form.get("date", "").strip()
        venue = request.form.get("venue", "").strip()
        description = request.form.get("description", "").strip()
        created_by = session["user"]["email"]
        photo = None
        if "photo" in request.files:
            file = request.files["photo"]
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
                photo = filename
        db.execute(
            "INSERT INTO events (name, date, venue, description, photo, created_by) VALUES (?, ?, ?, ?, ?, ?)",
            (name, date, venue, description, photo, created_by),
        )
        db.commit()
        flash("Your event has been posted publicly!", "success")
        return redirect(url_for("user_dashboard"))
    return render_template("user_add_event.html", user=session.get("user"))


# ---------- Events & Comments ----------
@app.route("/events")
def events_list():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "all")
    db = get_db()
    rows = db.execute("SELECT * FROM events ORDER BY date ASC").fetchall()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    events = []
    for e in rows:
        ev = dict(e)
        ev["created_by"] = e["created_by"] if "created_by" in e.keys() else "admin"
        ev["status"] = "upcoming" if ev["date"] > today else ("ongoing" if ev["date"] == today else "completed")
        events.append(ev)
    if q:
        q_low = q.lower()
        events = [ev for ev in events if q_low in ev["name"].lower() or q_low in ev["venue"].lower()]
    if status in ("upcoming", "ongoing", "completed"):
        events = [ev for ev in events if ev["status"] == status]
    return render_template("events.html", events=events, q=q, status=status, user=session.get("user"))


@app.route("/event/<int:event_id>", methods=["GET", "POST"])
def event_detail(event_id):
    db = get_db()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        flash("Event not found", "warning")
        return redirect(url_for("events_list"))

    if request.method == "POST":
        if "user" not in session:
            flash("Please log in to comment.", "warning")
            return redirect(url_for("user_login"))
        comment_text = request.form.get("comment", "").strip()
        if comment_text:
            db.execute(
                "INSERT INTO comments (event_id, user_id, comment) VALUES (?, ?, ?)",
                (event_id, session["user"]["id"], comment_text),
            )
            db.commit()
            flash("Comment added successfully!", "success")
        return redirect(url_for("event_detail", event_id=event_id))

    comments = db.execute(
        """
        SELECT c.id, c.comment, c.timestamp, u.username
        FROM comments c
        JOIN users u ON c.user_id = u.id
        WHERE c.event_id = ?
        ORDER BY c.timestamp DESC
        """,
        (event_id,),
    ).fetchall()
    return render_template("event_detail.html", event=event, comments=comments, user=session.get("user"))

@app.route("/add_cohost", methods=["POST"])
def add_cohost():
    event_id = request.form.get("event_id")
    name = request.form.get("cohost_name")
    email = request.form.get("cohost_email")

    if not all([event_id, name, email]):
        flash("All fields are required!", "danger")
        return redirect(url_for("event_detail", event_id=event_id))

    db = get_db()
    db.execute(
        "INSERT INTO cohosts (event_id, name, email) VALUES (?, ?, ?)",
        (event_id, name, email)
    )
    db.commit()
    db.close()

    flash("Co-host added successfully!", "success")
    return redirect(url_for("event_detail", event_id=event_id))

# ---------- Registration ----------
@app.route("/register", methods=["GET", "POST"], endpoint="register")
def register_event():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        event_id = request.form.get("event_id")
        if not (name and email and event_id):
            flash("Please fill all fields", "warning")
            return redirect(url_for("register"))
        existing = db.execute("SELECT * FROM participants WHERE email=? AND event_id=?", (email, event_id)).fetchone()
        if existing:
            flash("You already registered for this event.", "info")
            session["user_email"] = email
            return redirect(url_for("my_registrations"))
        db.execute("INSERT INTO participants (name, email, event_id) VALUES (?, ?, ?)", (name, email, event_id))
        db.commit()
        session["user_email"] = email
        flash("Registered successfully!", "success")
        return redirect(url_for("my_registrations"))
    events = db.execute("SELECT * FROM events ORDER BY date ASC").fetchall()
    return render_template("register.html", events=events, user=session.get("user"))


@app.route("/my-registrations", methods=["GET", "POST"])
def my_registrations():
    db = get_db()
    user_email = session.get("user_email")
    if not user_email:
        flash("Please register or log in first.", "warning")
        return redirect(url_for("register"))
    if request.method == "POST":
        reg_id = request.form.get("reg_id")
        if reg_id:
            db.execute("DELETE FROM participants WHERE id=?", (reg_id,))
            db.commit()
            flash("Registration cancelled.", "info")
            return redirect(url_for("my_registrations"))
    regs = db.execute(
        """
        SELECT p.id, e.name, e.date, e.venue, e.photo
        FROM participants p
        JOIN events e ON p.event_id = e.id
        WHERE p.email=?
        ORDER BY e.date ASC
        """,
        (user_email,),
    ).fetchall()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    registrations = []
    for r in regs:
        status = "upcoming" if r["date"] > today else ("ongoing" if r["date"] == today else "completed")
        registrations.append({**dict(r), "status": status})
    return render_template("my_registrations.html", registrations=registrations, user_email=user_email, user=session.get("user"))


# ---------- Chatbot ----------
@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    msg = data.get("message", "")
    db = get_db()
    evs = db.execute("SELECT name, date, venue FROM events ORDER BY date ASC LIMIT 10").fetchall()
    event_summary = "\n".join([f"{e['name']} on {e['date']} at {e['venue']}" for e in evs])
    prompt = f"You are Smart Event Assistant. Upcoming events:\n{event_summary}\nUser says: {msg}"

    reply = "Sorry — the assistant is unavailable right now."
    if client:
        try:
            res = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": "You are a friendly event assistant."},
                          {"role": "user", "content": prompt}],
                max_tokens=150,
            )
            reply = res.choices[0].message.content.strip()
        except Exception as e:
            reply = f"Error: {e}"

    return jsonify({"reply": reply})


# ---------- RUN ----------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True)

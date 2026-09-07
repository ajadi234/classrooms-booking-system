import os
from flask import Flask, render_template, request, redirect, session, flash
from flask_wtf import CSRFProtect
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
import psycopg
from psycopg.rows import dict_row
import secrets
from datetime import datetime

app = Flask(__name__)

# ==================================================
# SECRET KEY (fix: no longer hardcoded)
# ==================================================
# In production, set SECRET_KEY as an environment variable.
# Falls back to a random key for local dev (sessions reset on restart).
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ==================================================
# EMAIL CONFIG (for password reset)
# ==================================================
app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "youraddress@gmail.com")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD", "your16digitapppassword")
app.config["MAIL_DEFAULT_SENDER"] = app.config["MAIL_USERNAME"]
mail = Mail(app)

# Serializer for generating secure, expiring password-reset tokens
reset_serializer = URLSafeTimedSerializer(app.secret_key)
RESET_TOKEN_MAX_AGE_SECONDS = 1800  # 30 minutes

# ==================================================
# CSRF PROTECTION (fix: added)
# ==================================================
csrf = CSRFProtect(app)


# ==================================================
# CLASSROOMS / LECTURE HALLS
# ==================================================
# fix: halls now live in the database (see classrooms table) so the
# Admin can add new buildings/halls without editing this file.
# This list is only used ONCE, to seed the table the first time it's created.

DEFAULT_CLASSROOMS = [
    {"name": "Multipurpose Hall", "capacity": 4000, "location": "Main Campus"},
    {"name": "Grace Akinlola Hall", "capacity": 2000, "location": "Lecture Theatre"},
    {"name": "Sawe Hall", "capacity": 2000, "location": "Lecture Theatre"},
    {"name": "Adegboyegba Hall (AOH)", "capacity": 3000, "location": "Faculty of Computing"},
    {"name": "ETF Science Hall", "capacity": 1000, "location": "Faculty of Science"},
    {"name": "Science Lecture Hall 1", "capacity": 100, "location": "Faculty of Science"},
    {"name": "Science Lecture Hall 2", "capacity": 100, "location": "Faculty of Science"},
    {"name": "Engineering Studio Centre", "capacity": 70, "location": "Faculty of Engineering"},
    {"name": "Faculty of Education Hall 1", "capacity": 300, "location": "Faculty of Education"},
    {"name": "Faculty of Education Hall 2", "capacity": 300, "location": "Faculty of Education"},
    {"name": "Faculty of Nursing Hall A", "capacity": 300, "location": "Faculty of Nursing"},
    {"name": "Faculty of Nursing Hall B", "capacity": 300, "location": "Faculty of Nursing"},
    {"name": "Faculty of Nursing Hall C", "capacity": 300, "location": "Faculty of Nursing"},
    {"name": "Faculty of Nursing Auditorium", "capacity": 2000, "location": "Faculty of Nursing"},
]

# Role allow-list (fix: prevents privilege escalation via registration form)
ALLOWED_SELF_REGISTER_ROLES = {"Student", "Lecturer"}


# ==================================================
# DATABASE CONNECTION
# ==================================================
# fix: migrated from sqlite3 to PostgreSQL so data survives redeploys on
# hosts (like Render's free tier) that don't offer persistent disks.
# DATABASE_URL is provided automatically by Render when you attach a
# Postgres database to this web service. For local development, set it
# yourself, e.g.:
#   export DATABASE_URL="postgresql://user:password@localhost:5432/classroom"

DATABASE_URL = os.environ.get("DATABASE_URL")


class DBWrapper:
    """Thin wrapper so the rest of the app can keep calling db.execute(...)
    the same way it did with sqlite3, but backed by psycopg (v3) + Postgres.
    Rows come back as dict-like objects, so booking["field"] and
    booking['field'] access in templates keeps working unchanged."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=()):
        # sqlite3 used "?" placeholders; psycopg/Postgres uses "%s"
        query = query.replace("?", "%s")
        cur = self.conn.cursor()
        try:
            cur.execute(query, params)
        except psycopg.Error:
            self.conn.rollback()
            raise
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it to your Postgres connection "
            "string (Render provides this automatically once you attach a "
            "Postgres database to this web service)."
        )
    conn = psycopg.connect(DATABASE_URL, sslmode="require", row_factory=dict_row)
    return DBWrapper(conn)


# ==================================================
# CREATE DATABASE TABLES
# ==================================================

def create_table():
    db = get_db()

    # fix: AUTOINCREMENT (sqlite) -> SERIAL (Postgres) for auto-incrementing IDs
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            fullname TEXT NOT NULL,
            email TEXT UNIQUE,
            matric_no TEXT UNIQUE,
            department TEXT,
            level TEXT,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('Student', 'Lecturer', 'Admin'))
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            room TEXT NOT NULL,
            course_name TEXT,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            purpose TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    # fix: classrooms now live in the database so the Admin can add new
    # halls/buildings without editing the code.
    db.execute("""
        CREATE TABLE IF NOT EXISTS classrooms (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            capacity INTEGER NOT NULL,
            location TEXT NOT NULL
        )
    """)

    # Seed the classrooms table with the original hall list, but only
    # the very first time (i.e. if the table is currently empty).
    existing_count = db.execute("SELECT COUNT(*) AS c FROM classrooms").fetchone()["c"]
    if existing_count == 0:
        for room in DEFAULT_CLASSROOMS:
            db.execute(
                "INSERT INTO classrooms (name, capacity, location) VALUES (?, ?, ?)",
                (room["name"], room["capacity"], room["location"])
            )

    # Default Admin (fix: hashed password, fixed so you always know it)
    admin = db.execute(
        "SELECT * FROM users WHERE email = ?",
        ("admin@school.com",)
    ).fetchone()

    if admin is None:
        # CHANGE THIS to your own password before deploying for real use.
        default_admin_password = os.environ.get("ADMIN_PASSWORD", "Admin@12345")
        print("=" * 60)
        print("Default admin account created:")
        print(f"   email: admin@school.com")
        print(f"   password: {default_admin_password}")
        print("Change this password after logging in for the first time.")
        print("=" * 60)

        db.execute(
            """
            INSERT INTO users (fullname, email, password, role)
            VALUES (?, ?, ?, ?)
            """,
            ("System Administrator", "admin@school.com",
             generate_password_hash(default_admin_password), "Admin")
        )

    db.commit()
    db.close()


# ==================================================
# TIME OVERLAP HELPER
# ==================================================

def times_overlap(start_a, end_a, start_b, end_b):
    """Return True if time range A overlaps time range B (HH:MM strings)."""
    return start_a < end_b and start_b < end_a


def get_classrooms():
    """Fetch the current list of lecture halls from the database."""
    db = get_db()
    rooms = db.execute("SELECT * FROM classrooms ORDER BY name").fetchall()
    db.close()
    return rooms


# ==================================================
# HOME
# ==================================================

@app.route("/")
def home():
    return render_template("index.html")


# ==================================================
# REGISTER
# ==================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        fullname = request.form.get("fullname")
        email = request.form.get("email")
        matric_no = request.form.get("matric_no")
        department = request.form.get("department")
        level = request.form.get("level")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")
        role = request.form.get("role")

        # fix: role allow-list — Admin can never self-register
        if role not in ALLOWED_SELF_REGISTER_ROLES:
            return """
                <h2>Invalid role selected.</h2>
                <a href="/register">Try Again</a>
            """

        if not password or password != confirm_password:
            return """
                <h2>Passwords do not match.</h2>
                <a href="/register">Try Again</a>
            """

        # fix: hash password before storing
        hashed_password = generate_password_hash(password)

        db = get_db()

        try:
            if role == "Student":
                if not matric_no or not department or not level:
                    db.close()
                    return """
                        <h2>Please fill Matric Number, Department and Level.</h2>
                        <a href="/register">Try Again</a>
                    """
                db.execute(
                    """
                    INSERT INTO users (fullname, matric_no, department, level, password, role)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (fullname, matric_no, department, level, hashed_password, role)
                )
            else:
                if not email:
                    db.close()
                    return """
                        <h2>Email is required for Lecturer.</h2>
                        <a href="/register">Try Again</a>
                    """
                db.execute(
                    """
                    INSERT INTO users (fullname, email, password, role)
                    VALUES (?, ?, ?, ?)
                    """,
                    (fullname, email, hashed_password, role)
                )

            db.commit()
        except psycopg.IntegrityError:
            db.close()
            return """
                <h2>Email or Matric Number already registered.</h2>
                <a href="/register">Try Again</a>
            """

        db.close()
        return redirect("/login")

    return render_template("register.html")


# ==================================================
# LOGIN (fix: password checked via hash, not in SQL)
# ==================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_id = request.form.get("login_id")
        password = request.form.get("password")

        db = get_db()

        user = db.execute(
            """
            SELECT * FROM users
            WHERE (email = ? OR matric_no = ?)
            """,
            (login_id, login_id)
        ).fetchone()

        db.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["fullname"] = user["fullname"]
            session["role"] = user["role"]

            if user["role"] == "Admin":
                return redirect("/admin")
            if user["role"] == "Lecturer":
                return redirect("/lecturer")
            if user["role"] == "Student":
                return redirect("/student")

            return redirect("/dashboard")

        return """
            <h2>Invalid login details.</h2>
            <a href="/login">Try Again</a>
        """

    return render_template("login.html")


# ==================================================
# FORGOT PASSWORD (request a reset link by email)
# ==================================================

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        db.close()

        # Always show the same message, whether or not the email exists.
        # This prevents someone from using this form to discover which
        # emails are registered in the system.
        generic_message = """
            <h2>Check Your Email</h2>
            <p>If an account with that email exists, a password reset link has been sent.</p>
            <a href="/login">Back to Login</a>
        """

        if user:
            token = reset_serializer.dumps(user["email"], salt="password-reset")
            reset_url = request.url_root.rstrip("/") + "/reset-password/" + token

            try:
                msg = Message(
                    subject="Password Reset - Classroom Booking System",
                    recipients=[user["email"]],
                    body=(
                        f"Hello {user['fullname']},\n\n"
                        f"Click the link below to reset your password. "
                        f"This link expires in 30 minutes.\n\n"
                        f"{reset_url}\n\n"
                        f"If you did not request this, you can ignore this email."
                    )
                )
                mail.send(msg)
            except Exception as e:
                # Email sending failed (e.g. bad SMTP credentials) — don't
                # expose the error to the user, just log it server-side.
                print(f"Failed to send reset email: {e}")

        return generic_message

    return render_template("forgot_password.html")


# ==================================================
# RESET PASSWORD (using the emailed token)
# ==================================================

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        email = reset_serializer.loads(
            token, salt="password-reset", max_age=RESET_TOKEN_MAX_AGE_SECONDS
        )
    except SignatureExpired:
        return """
            <h2>Link Expired</h2>
            <p>This password reset link has expired. Please request a new one.</p>
            <a href="/forgot-password">Request New Link</a>
        """
    except BadSignature:
        return """
            <h2>Invalid Link</h2>
            <p>This password reset link is not valid.</p>
            <a href="/forgot-password">Request New Link</a>
        """

    if request.method == "POST":
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")

        if not new_password or new_password != confirm_password:
            return """
                <h2>Passwords do not match.</h2>
                <a href="javascript:history.back()">Try Again</a>
            """

        db = get_db()
        db.execute(
            "UPDATE users SET password = ? WHERE email = ?",
            (generate_password_hash(new_password), email)
        )
        db.commit()
        db.close()

        return """
            <h2>Password Updated</h2>
            <p>Your password has been reset. You can now log in.</p>
            <a href="/login">Go to Login</a>
        """

    return render_template("reset_password.html", token=token)


# ==================================================
# DASHBOARD
# ==================================================

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    bookings = db.execute(
        """
        SELECT bookings.*, users.fullname AS booked_by
        FROM bookings
        LEFT JOIN users ON bookings.user_id = users.id
        ORDER BY date, start_time
        """
    ).fetchall()
    db.close()

    return render_template("dashboard.html", classrooms=get_classrooms(), bookings=bookings)


# ==================================================
# ADMIN
# ==================================================

@app.route("/admin")
def admin():
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") != "Admin":
        return """
            <h2>Access Denied</h2>
            <p>You are not authorized to access the Admin Dashboard.</p>
            <a href="/dashboard">Back to Dashboard</a>
        """

    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY id DESC").fetchall()

    # fix: support filtering the bookings table by room, date, or booked-by name
    search_room = request.args.get("search_room", "").strip()
    search_date = request.args.get("search_date", "").strip()
    search_name = request.args.get("search_name", "").strip()

    query = """
        SELECT bookings.*, users.fullname AS booked_by
        FROM bookings
        LEFT JOIN users ON bookings.user_id = users.id
        WHERE 1=1
    """
    params = []

    if search_room:
        query += " AND bookings.room LIKE ?"
        params.append(f"%{search_room}%")

    if search_date:
        query += " AND bookings.date = ?"
        params.append(search_date)

    if search_name:
        query += " AND users.fullname LIKE ?"
        params.append(f"%{search_name}%")

    query += " ORDER BY bookings.date, bookings.start_time"

    bookings = db.execute(query, params).fetchall()
    db.close()

    return render_template(
        "admin.html",
        users=users,
        bookings=bookings,
        classrooms=get_classrooms(),
        search_room=search_room,
        search_date=search_date,
        search_name=search_name
    )


# ==================================================
# ADMIN: RESET A USER'S PASSWORD
# ==================================================

@app.route("/admin/reset-password/<int:user_id>", methods=["GET", "POST"])
def admin_reset_password(user_id):
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") != "Admin":
        return """
            <h2>Access Denied</h2>
            <p>Only an Admin can reset passwords.</p>
            <a href="/dashboard">Back to Dashboard</a>
        """

    db = get_db()
    target_user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    if not target_user:
        db.close()
        return "User not found"

    if request.method == "POST":
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")

        if not new_password or new_password != confirm_password:
            db.close()
            return """
                <h2>Passwords do not match.</h2>
                <a href="javascript:history.back()">Try Again</a>
            """

        db.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (generate_password_hash(new_password), user_id)
        )
        db.commit()
        db.close()
        return redirect("/admin")

    db.close()
    return render_template("admin_reset_password.html", target_user=target_user)


# ==================================================
# ADMIN: MANAGE LECTURE HALLS
# ==================================================

@app.route("/admin/classrooms/add", methods=["POST"])
def add_classroom():
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") != "Admin":
        return """
            <h2>Access Denied</h2>
            <p>Only an Admin can manage lecture halls.</p>
            <a href="/dashboard">Back to Dashboard</a>
        """

    name = request.form.get("name", "").strip()
    location = request.form.get("location", "").strip()
    capacity = request.form.get("capacity", "").strip()

    if not name or not location or not capacity:
        flash("All fields (Name, Location, Capacity) are required to add a hall.", "error")
        return redirect("/admin")

    try:
        capacity = int(capacity)
        if capacity <= 0:
            raise ValueError
    except ValueError:
        flash("Capacity must be a positive number.", "error")
        return redirect("/admin")

    db = get_db()
    try:
        db.execute(
            "INSERT INTO classrooms (name, capacity, location) VALUES (?, ?, ?)",
            (name, capacity, location)
        )
        db.commit()
        flash(f"Lecture hall '{name}' added successfully.", "success")
    except psycopg.IntegrityError:
        flash(f"A hall named '{name}' already exists.", "error")
    finally:
        db.close()

    return redirect("/admin")


@app.route("/admin/classrooms/delete/<int:room_id>", methods=["POST"])
def delete_classroom(room_id):
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") != "Admin":
        return """
            <h2>Access Denied</h2>
            <p>Only an Admin can manage lecture halls.</p>
            <a href="/dashboard">Back to Dashboard</a>
        """

    db = get_db()
    room = db.execute("SELECT * FROM classrooms WHERE id = ?", (room_id,)).fetchone()

    if not room:
        db.close()
        return "Lecture hall not found"

    db.execute("DELETE FROM classrooms WHERE id = ?", (room_id,))
    db.commit()
    db.close()

    flash(f"Lecture hall '{room['name']}' removed.", "success")
    return redirect("/admin")


@app.route("/admin/classrooms/edit/<int:room_id>", methods=["GET", "POST"])
def edit_classroom(room_id):
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") != "Admin":
        return """
            <h2>Access Denied</h2>
            <p>Only an Admin can manage lecture halls.</p>
            <a href="/dashboard">Back to Dashboard</a>
        """

    db = get_db()
    room = db.execute("SELECT * FROM classrooms WHERE id = ?", (room_id,)).fetchone()

    if not room:
        db.close()
        return "Lecture hall not found"

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        capacity = request.form.get("capacity", "").strip()

        if not name or not location or not capacity:
            db.close()
            flash("All fields are required.", "error")
            return redirect(f"/admin/classrooms/edit/{room_id}")

        try:
            capacity = int(capacity)
            if capacity <= 0:
                raise ValueError
        except ValueError:
            db.close()
            flash("Capacity must be a positive number.", "error")
            return redirect(f"/admin/classrooms/edit/{room_id}")

        try:
            db.execute(
                "UPDATE classrooms SET name = ?, capacity = ?, location = ? WHERE id = ?",
                (name, capacity, location, room_id)
            )
            db.commit()
            flash(f"Lecture hall '{name}' updated.", "success")
        except psycopg.IntegrityError:
            flash(f"A hall named '{name}' already exists.", "error")
        finally:
            db.close()

        return redirect("/admin")

    db.close()
    return render_template("edit_classroom.html", room=room)


# ==================================================
# LECTURER
# ==================================================

@app.route("/lecturer")
def lecturer():
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") != "Lecturer":
        return """
            <h2>Access Denied</h2>
            <p>Only lecturers can access this page.</p>
            <a href="/dashboard">Back to Dashboard</a>
        """

    db = get_db()

    check_date = request.args.get("check_date") or ""
    check_start_time = request.args.get("check_start_time") or ""
    check_end_time = request.args.get("check_end_time") or ""
    booked_rooms = []

    if check_date and check_start_time and check_end_time:
        same_day_bookings = db.execute(
            "SELECT room, start_time, end_time FROM bookings WHERE date = ?",
            (check_date,)
        ).fetchall()
        booked_rooms = [
            row["room"] for row in same_day_bookings
            if times_overlap(check_start_time, check_end_time, row["start_time"], row["end_time"])
        ]

    bookings = db.execute(
        """
        SELECT bookings.*, users.fullname AS booked_by
        FROM bookings
        LEFT JOIN users ON bookings.user_id = users.id
        ORDER BY date, start_time
        """
    ).fetchall()

    db.close()

    return render_template(
        "lecturer.html",
        classrooms=get_classrooms(),
        bookings=bookings,
        check_date=check_date,
        check_start_time=check_start_time,
        check_end_time=check_end_time,
        booked_rooms=booked_rooms
    )


# ==================================================
# STUDENT
# ==================================================

@app.route("/student")
def student():
    if "user_id" not in session:
        return redirect("/login")

    if session.get("role") != "Student":
        return """
            <h2>Access Denied</h2>
            <p>Only students can access this page.</p>
            <a href="/dashboard">Back to Dashboard</a>
        """

    db = get_db()
    bookings = db.execute(
        """
        SELECT bookings.*, users.fullname AS booked_by
        FROM bookings
        LEFT JOIN users ON bookings.user_id = users.id
        ORDER BY date, start_time
        """
    ).fetchall()
    db.close()

    return render_template(
        "student.html",
        classrooms=get_classrooms(),
        bookings=bookings
    )


# ==================================================
# BOOK (fix: now stores course_name)
# ==================================================

@app.route("/book", methods=["POST"])
def book():
    if "user_id" not in session:
        return redirect("/login")

    room = request.form["room"]
    course_name = request.form.get("course_name")
    date = request.form["date"]
    start_time = request.form["start_time"]
    end_time = request.form["end_time"]
    purpose = request.form["purpose"]
    user_id = session["user_id"]

    # fix: end time must be after start time
    if end_time <= start_time:
        return """
            <h2>Invalid Time Range</h2>
            <p>End time must be after start time.</p>
            <a href="javascript:history.back()">Go Back</a>
        """

    # fix: reject bookings for a date/time already in the past
    try:
        booking_start_dt = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
        if booking_start_dt < datetime.now():
            return """
                <h2>Invalid Date/Time</h2>
                <p>You cannot book a classroom in the past.</p>
                <a href="javascript:history.back()">Go Back</a>
            """
    except ValueError:
        return """
            <h2>Invalid Date/Time Format</h2>
            <a href="javascript:history.back()">Go Back</a>
        """

    db = get_db()

    same_day_bookings = db.execute(
        "SELECT * FROM bookings WHERE room = ? AND date = ?",
        (room, date)
    ).fetchall()

    for existing in same_day_bookings:
        if times_overlap(start_time, end_time, existing["start_time"], existing["end_time"]):
            db.close()
            return f"""
                <h2>Classroom Already Booked!</h2>
                <p>This hall is already booked from {existing['start_time']} to {existing['end_time']} on {date}.</p>
                <a href="javascript:history.back()">Go Back</a>
            """

    db.execute(
        "INSERT INTO bookings (user_id, room, course_name, date, start_time, end_time, purpose) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, room, course_name, date, start_time, end_time, purpose)
    )
    db.commit()
    db.close()

    flash(f"Booking confirmed: {room} on {date} from {start_time} to {end_time}.", "success")

    role = session.get("role")
    if role == "Admin":
        return redirect("/admin")
    elif role == "Lecturer":
        return redirect("/lecturer")
    elif role == "Student":
        return redirect("/student")
    return redirect("/dashboard")


# ==================================================
# EDIT BOOKING
# ==================================================

@app.route("/edit/<int:booking_id>", methods=["GET", "POST"])
def edit_booking(booking_id):
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()

    if not booking:
        db.close()
        return "Booking not found"

    if booking["user_id"] != session["user_id"] and session.get("role") != "Admin":
        db.close()
        return """
            <h2>Access Denied</h2>
            <p>You can only edit your own bookings.</p>
            <a href="javascript:history.back()">Go Back</a>
        """

    if request.method == "POST":
        room = request.form["room"]
        course_name = request.form.get("course_name")
        date = request.form["date"]
        start_time = request.form["start_time"]
        end_time = request.form["end_time"]
        purpose = request.form["purpose"]

        if end_time <= start_time:
            db.close()
            return """
                <h2>Invalid Time Range</h2>
                <p>End time must be after start time.</p>
                <a href="javascript:history.back()">Go Back</a>
            """

        try:
            booking_start_dt = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            if booking_start_dt < datetime.now():
                db.close()
                return """
                    <h2>Invalid Date/Time</h2>
                    <p>You cannot book a classroom in the past.</p>
                    <a href="javascript:history.back()">Go Back</a>
                """
        except ValueError:
            db.close()
            return """
                <h2>Invalid Date/Time Format</h2>
                <a href="javascript:history.back()">Go Back</a>
            """

        same_day_bookings = db.execute(
            "SELECT * FROM bookings WHERE room = ? AND date = ? AND id != ?",
            (room, date, booking_id)
        ).fetchall()

        for existing in same_day_bookings:
            if times_overlap(start_time, end_time, existing["start_time"], existing["end_time"]):
                db.close()
                return f"""
                    <h2>Classroom Already Booked!</h2>
                    <p>This hall is already booked from {existing['start_time']} to {existing['end_time']} on {date}.</p>
                    <a href="javascript:history.back()">Go Back</a>
                """

        db.execute(
            "UPDATE bookings SET room = ?, course_name = ?, date = ?, start_time = ?, end_time = ?, purpose = ? WHERE id = ?",
            (room, course_name, date, start_time, end_time, purpose, booking_id)
        )
        db.commit()
        db.close()

        flash(f"Booking updated: {room} on {date} from {start_time} to {end_time}.", "success")

        role = session.get("role")
        if role == "Admin":
            return redirect("/admin")
        elif role == "Lecturer":
            return redirect("/lecturer")
        elif role == "Student":
            return redirect("/student")
        return redirect("/dashboard")

    db.close()
    return render_template("edit_booking.html", booking=booking, classrooms=get_classrooms())


# ==================================================
# DELETE (fix: POST only, CSRF-protected by default)
# ==================================================

@app.route("/delete/<int:booking_id>", methods=["POST"])
def delete_booking(booking_id):
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()

    if not booking:
        db.close()
        return "Booking not found"

    if booking["user_id"] != session["user_id"] and session.get("role") != "Admin":
        db.close()
        return """
            <h2>Access Denied</h2>
            <p>You can only delete your own bookings.</p>
            <a href="javascript:history.back()">Go Back</a>
        """

    db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    db.commit()
    db.close()

    flash("Booking deleted.", "success")

    role = session.get("role")
    if role == "Admin":
        return redirect("/admin")
    elif role == "Lecturer":
        return redirect("/lecturer")
    elif role == "Student":
        return redirect("/student")
    return redirect("/dashboard")


# ==================================================
# LOGOUT
# ==================================================

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ==================================================
# START
# ==================================================

if __name__ == "__main__":
    create_table()
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode)
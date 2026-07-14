"""
app.py — AI-Based Interview Security System (Flask backend)

Free stack: Flask + SQLite/Postgres + DeepFace + Jitsi (meet.jit.si, no self-hosting needed)

Run locally:
    cp .env.example .env      # then edit ADMIN_PASSWORD_HASH etc.
    pip install -r requirements.txt --break-system-packages
    python app.py
"""
import os
import base64
import secrets
import threading
import numpy as np
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory, flash
from werkzeug.utils import secure_filename
from flask_login import login_required, login_user, logout_user, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import database as db
from auth import login_manager, verify_admin, AdminUser

load_dotenv()

app = Flask(__name__)

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    # OK for local dev only — every restart would log everyone out otherwise.
    # In production this MUST be set explicitly (see .env.example) or sessions
    # become unusable across the multiple worker processes gunicorn spawns.
    SECRET_KEY = secrets.token_hex(16)
app.secret_key = SECRET_KEY

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}
MAX_UPLOAD_MB = 8
COSINE_THRESHOLD = 0.35
USE_MODEL = "ArcFace"

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
db.init_db()

login_manager.init_app(app)

limiter = Limiter(get_remote_address, app=app, default_limits=["200 per hour"])

# DeepFace inference is CPU-heavy (1-3s per call). On a free-tier instance,
# letting unlimited requests hit it concurrently is what actually crashes
# the server under real public traffic — not a lack of code, just CPU
# contention. This caps concurrent face-matching so extra requests queue
# briefly instead of taking the whole process down.
_deepface_semaphore = threading.Semaphore(3)

_deepface = None


def get_deepface():
    global _deepface
    if _deepface is None:
        from deepface import DeepFace
        _deepface = DeepFace
    return _deepface


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def is_genuine_image(file_bytes):
    """
    Don't trust the file extension alone — verify the bytes actually decode
    as an image before ever handing them to DeepFace/TensorFlow. Cheap check,
    closes an easy abuse/DoS vector on a public-facing upload endpoint.
    """
    try:
        img = Image.open(BytesIO(file_bytes))
        img.verify()
        return True
    except Exception:
        return False


def cosine_distance(a, b):
    a = np.array(a, dtype=np.float32).flatten()
    b = np.array(b, dtype=np.float32).flatten()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 1.0
    sim = np.dot(a, b) / (na * nb)
    return 1.0 - float(np.clip(sim, -1.0, 1.0))


def compute_embedding_from_path(path):
    DeepFace = get_deepface()
    with _deepface_semaphore:
        reps = DeepFace.represent(img_path=path, model_name=USE_MODEL, enforce_detection=True)
    return reps[0]["embedding"]


def compute_embedding_from_base64(b64_string):
    DeepFace = get_deepface()
    header, encoded = b64_string.split(",", 1) if "," in b64_string else ("", b64_string)
    img_bytes = base64.b64decode(encoded)

    if not is_genuine_image(img_bytes):
        return None, None

    tmp_path = os.path.join(UPLOAD_FOLDER, f"_tmp_{secrets.token_hex(6)}.jpg")
    with open(tmp_path, "wb") as f:
        f.write(img_bytes)
    try:
        with _deepface_semaphore:
            reps = DeepFace.represent(img_path=tmp_path, model_name=USE_MODEL, enforce_detection=True)
        return reps[0]["embedding"], tmp_path
    except Exception:
        return None, tmp_path


def find_best_match(live_embedding):
    """Checks against EVERY stored photo of EVERY user — best (lowest distance) match wins."""
    all_photos = db.get_all_photo_embeddings()
    best, best_dist = None, 1.0
    for record in all_photos:
        d = cosine_distance(live_embedding, record["embedding"])
        if d < best_dist:
            best_dist, best = d, record
    if best is not None and best_dist <= COSINE_THRESHOLD:
        return best, best_dist
    return None, best_dist


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    # reference photos and snapshots contain faces — admin-only, not public
    return send_from_directory(UPLOAD_FOLDER, filename)


# ============================================================
# Public / candidate & interviewer facing routes
# ============================================================
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/join/<meeting_code>")
def join_meeting(meeting_code):
    meeting = db.get_meeting(meeting_code)
    if not meeting:
        return render_template("index.html", error="Invalid or expired meeting link.")
    return render_template("verify.html", meeting=meeting)


@app.route("/api/verify", methods=["POST"])
@limiter.limit("15 per minute")
def api_verify():
    data = request.get_json(silent=True) or {}
    b64_img = data.get("image")
    meeting_code = data.get("meeting_code")
    if not b64_img:
        return jsonify({"status": "error", "message": "No image supplied"}), 400

    embedding, tmp_path = compute_embedding_from_base64(b64_img)

    if embedding is None:
        return jsonify({"status": "no_face", "message": "No face detected. Try again."})

    match, distance = find_best_match(embedding)

    if match:
        db.add_log(meeting_code, match["user_id"], "verified",
                    f"distance={distance:.3f} via photo={os.path.basename(match['photo_path'])}")
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        return jsonify({
            "status": "matched", "user_id": match["user_id"],
            "name": match["name"], "role": match["role"],
            "distance": round(distance, 3),
        })
    else:
        db.add_log(meeting_code, None, "denied", f"best_distance={distance:.3f}")
        return jsonify({
            "status": "denied", "distance": round(distance, 3),
            "snapshot_tmp": os.path.basename(tmp_path) if tmp_path else None,
        })


@app.route("/api/request-review", methods=["POST"])
@limiter.limit("5 per minute")
def api_request_review():
    data = request.get_json(silent=True) or {}
    meeting_code = data.get("meeting_code")
    claimed_name = (data.get("claimed_name") or "Unknown")[:100]
    snapshot_tmp = data.get("snapshot_tmp")

    # Don't let a second request stack up while one is already awaiting admin action.
    if db.has_pending_for_meeting(meeting_code):
        return jsonify({
            "status": "already_pending",
            "message": "A review request is already pending for this meeting.",
        }), 409

    # Cap total requests per meeting at 2 (initial + one retry).
    if db.count_review_requests(meeting_code) >= 2:
        return jsonify({
            "status": "limit_reached",
            "message": "Maximum review requests reached for this meeting. "
                       "Please contact your interviewer/HR to reschedule.",
        }), 429

    snapshot_path = os.path.join(UPLOAD_FOLDER, snapshot_tmp) if snapshot_tmp else None
    req_id = db.add_pending_approval(meeting_code, claimed_name, snapshot_path)
    db.add_log(meeting_code, None, "manual_review_requested", f"claimed_name={claimed_name}")
    return jsonify({"status": "pending", "request_id": req_id})


@app.route("/api/check-approval/<int:request_id>")
def api_check_approval(request_id):
    approval = db.get_approval(request_id)
    if not approval:
        return jsonify({"status": "not_found"})
    return jsonify({"status": approval["status"]})


@app.route("/meeting/<meeting_code>")
def meeting_room(meeting_code):
    role = request.args.get("role", "candidate")
    name = request.args.get("name", "Guest")[:100]
    meeting = db.get_meeting(meeting_code)
    if not meeting:
        return redirect(url_for("home"))
    db.add_log(meeting_code, None, "joined", f"name={name} role={role}")
    return render_template("meeting.html", meeting=meeting, role=role, name=name)


@app.route("/api/log-event", methods=["POST"])
@limiter.limit("30 per minute")
def api_log_event():
    data = request.get_json(silent=True) or {}
    db.add_log(data.get("meeting_code"), data.get("user_id"),
               data.get("event_type"), data.get("details", ""))
    return jsonify({"status": "ok"})


# ============================================================
# Admin auth
# ============================================================
@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if verify_admin(username, password):
            login_user(AdminUser(username))
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_login.html", error="Invalid credentials")
    return render_template("admin_login.html")


@app.route("/admin/logout")
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for("admin_login"))


# ============================================================
# Admin panel routes — every one of these requires login now
# ============================================================
@app.route("/api/admin/pending-count")
@login_required
def api_admin_pending_count():
    return jsonify({"count": len(db.get_pending_approvals())})


@app.route("/admin")
@login_required
def admin_dashboard():
    users = db.get_all_users()
    meetings = db.get_all_meetings()
    pending = db.get_pending_approvals()
    return render_template("admin.html", users=users, meetings=meetings, pending=pending)


@app.route("/admin/add-user", methods=["POST"])
@login_required
def admin_add_user():
    name = request.form.get("name", "")[:100]
    role = request.form.get("role")
    email = request.form.get("email", "")[:150]
    photos = request.files.getlist("photos")

    if role not in ("interviewer", "candidate"):
        flash("Invalid role selected.", "danger")
        return redirect(url_for("admin_dashboard"))

    user_id = db.add_user(name, role, email)

    saved_count = 0
    failed = []
    for photo in photos:
        if not photo or photo.filename == "":
            continue
        if not allowed_file(photo.filename):
            failed.append(f"{photo.filename}: not a jpg/jpeg/png file")
            continue
        raw = photo.read()
        if not is_genuine_image(raw):
            failed.append(f"{photo.filename}: file is not a valid image")
            continue
        filename = secure_filename(f"user{user_id}_{secrets.token_hex(4)}_{photo.filename}")
        path = os.path.join(UPLOAD_FOLDER, filename)
        with open(path, "wb") as f:
            f.write(raw)
        try:
            embedding = compute_embedding_from_path(path)
            db.add_user_photo(user_id, path, embedding)
            saved_count += 1
        except Exception as e:
            failed.append(f"{photo.filename}: no clear face detected ({str(e)[:120]})")
            os.remove(path)

    if saved_count:
        flash(f"Added {name} with {saved_count} photo(s).", "success")
    if failed:
        flash(f"{len(failed)} photo(s) failed: " + " | ".join(failed), "warning")
    if saved_count == 0 and not failed:
        flash(f"Added {name}, but no photos were uploaded — they won't be matchable yet. Use '+ Add photo' below to add one.", "warning")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/add-photo/<int:user_id>", methods=["POST"])
@login_required
def admin_add_photo(user_id):
    photos = request.files.getlist("photos")
    saved_count = 0
    failed = []
    for photo in photos:
        if not photo or photo.filename == "":
            continue
        if not allowed_file(photo.filename):
            failed.append(f"{photo.filename}: not a jpg/jpeg/png file")
            continue
        raw = photo.read()
        if not is_genuine_image(raw):
            failed.append(f"{photo.filename}: file is not a valid image")
            continue
        filename = secure_filename(f"user{user_id}_{secrets.token_hex(4)}_{photo.filename}")
        path = os.path.join(UPLOAD_FOLDER, filename)
        with open(path, "wb") as f:
            f.write(raw)
        try:
            embedding = compute_embedding_from_path(path)
            db.add_user_photo(user_id, path, embedding)
            saved_count += 1
        except Exception as e:
            failed.append(f"{photo.filename}: no clear face detected ({str(e)[:120]})")
            os.remove(path)

    if saved_count:
        flash(f"Added {saved_count} photo(s).", "success")
    if failed:
        flash(f"{len(failed)} photo(s) failed: " + " | ".join(failed), "warning")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete-user/<int:user_id>", methods=["POST"])
@login_required
def admin_delete_user(user_id):
    db.delete_user(user_id)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete-photo/<int:photo_id>", methods=["POST"])
@login_required
def admin_delete_photo(photo_id):
    db.delete_photo(photo_id)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/create-meeting", methods=["POST"])
@login_required
def admin_create_meeting():
    title = request.form.get("title", "")[:150]
    interviewer_id = request.form.get("interviewer_id")
    candidate_id = request.form.get("candidate_id")
    meeting_code = secrets.token_hex(5)
    db.create_meeting(meeting_code, title, interviewer_id, candidate_id)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/approve/<int:request_id>", methods=["POST"])
@login_required
def admin_approve(request_id):
    db.resolve_approval(request_id, "approved")
    approval = db.get_approval(request_id)
    if approval:
        db.add_log(approval["meeting_code"], None, "manual_override_approved",
                   f"claimed_name={approval['claimed_name']}")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/deny/<int:request_id>", methods=["POST"])
@login_required
def admin_deny(request_id):
    db.resolve_approval(request_id, "denied")
    approval = db.get_approval(request_id)
    if approval:
        db.add_log(approval["meeting_code"], None, "manual_override_denied",
                   f"claimed_name={approval['claimed_name']}")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete-meeting/<meeting_code>", methods=["POST"])
@login_required
def admin_delete_meeting(meeting_code):
    db.delete_meeting(meeting_code)
    db.add_log(meeting_code, None, "meeting_deleted", "")
    flash("Meeting link deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logs")
@login_required
def admin_logs():
    logs = db.get_all_logs()
    return render_template("logs.html", logs=logs)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)

"""
auth.py
Single-admin authentication for the admin panel.
Credentials come from environment variables — NEVER hardcode them.
For V1 with "unknown scale, general public", one admin login is enough;
if multiple admin staff are needed later, this table-izes easily.
"""
import os
from werkzeug.security import check_password_hash, generate_password_hash
from flask_login import UserMixin, LoginManager

login_manager = LoginManager()
login_manager.login_view = "admin_login"


class AdminUser(UserMixin):
    def __init__(self, username):
        self.id = username


def _admin_username():
    return os.environ.get("ADMIN_USERNAME", "admin")


def _admin_password_hash():
    """
    Set ADMIN_PASSWORD_HASH in your environment, generated once via:
        python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password'))"
    Falling back to a default ONLY for local testing — the app refuses to
    start in production mode without this being explicitly set.
    """
    return os.environ.get("ADMIN_PASSWORD_HASH")


def verify_admin(username, password):
    if username != _admin_username():
        return False
    stored_hash = _admin_password_hash()
    if not stored_hash:
        return False
    return check_password_hash(stored_hash, password)


@login_manager.user_loader
def load_user(username):
    if username == _admin_username():
        return AdminUser(username)
    return None

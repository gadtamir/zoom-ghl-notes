import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import Employee


API_KEY_PREFIX = "zghl_"


def generate_api_key() -> tuple[str, str, str]:
    """Generate a fresh API key. Returns (full_key, prefix, sha256_hash).

    The full_key is shown to the admin ONCE — only the hash is stored.
    """
    raw = secrets.token_urlsafe(32)
    full_key = f"{API_KEY_PREFIX}{raw}"
    return full_key, full_key[: len(API_KEY_PREFIX) + 8], hash_api_key(full_key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_current_employee(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Employee:
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header")

    key_hash = hash_api_key(x_api_key)
    employee = db.query(Employee).filter(Employee.api_key_hash == key_hash).first()
    if employee is None or not employee.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or inactive API key")
    return employee

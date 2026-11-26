from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for
from livereload import Server

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BILLIN_DB_PATH", str(BASE_DIR / "billin.sqlite3")))
PUBLIC_BASE_URL = os.environ.get("BILLIN_PUBLIC_BASE_URL", "").rstrip("/")

MEMBER_COLORS = (
    "#2196F3",
    "#A800B7",
    "#5EA500",
    "#F59E0B",
    "#EF4444",
    "#14B8A6",
    "#8B5CF6",
    "#EC4899",
)

app = Flask(__name__)
app.debug = os.environ.get("FLASK_DEBUG", "1") == "1"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                token TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    finally:
        connection.close()


init_db()


def _load_group(token: str) -> dict | None:
    if not token:
        return None

    connection = _connect()
    try:
        row = connection.execute(
            "SELECT data FROM groups WHERE token = ?", (token,)
        ).fetchone()
        return json.loads(row["data"]) if row else None
    finally:
        connection.close()


def _insert_group(data: dict) -> None:
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO groups (token, data, revision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                data["token"],
                json.dumps(data, ensure_ascii=False),
                data["revision"],
                data["created_at"],
                data["updated_at"],
            ),
        )
    finally:
        connection.close()


def _mutate_group(token: str, mutator):
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT data, revision FROM groups WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            return None

        data = json.loads(row["data"])
        result = mutator(data)
        revision = int(row["revision"]) + 1
        data["revision"] = revision
        data["updated_at"] = _now()

        connection.execute(
            """
            UPDATE groups
            SET data = ?, revision = ?, updated_at = ?
            WHERE token = ?
            """,
            (
                json.dumps(data, ensure_ascii=False),
                revision,
                data["updated_at"],
                token,
            ),
        )
        connection.execute("COMMIT")
        return data, result
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _public_base_url() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL

    parsed = urlparse(request.host_url)
    host = parsed.hostname or ""
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "10.0.2.2"}:
        lan_ip = _lan_ip()
        if lan_ip:
            port = f":{parsed.port}" if parsed.port else ""
            return f"http://{lan_ip}{port}"

    return request.host_url.rstrip("/")


def _join_url(token: str) -> str:
    return f"{_public_base_url()}/join/{token}"


def _group_payload(data: dict) -> dict:
    group = json.loads(json.dumps(data, ensure_ascii=False))
    group["join_url"] = _join_url(data["token"])
    return group


def _clean_name(value) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    if not name:
        raise ApiError(400, "Nama tidak boleh kosong.")
    if len(name) > 40:
        raise ApiError(400, "Nama maksimal 40 karakter.")
    return name


def _to_number(value, default=0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if number.is_integer() else number


def _to_int(value, default=0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_receipt(raw) -> dict:
    if not isinstance(raw, dict):
        raise ApiError(400, "Data struk tidak valid.")

    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        raw_items = []

    items = []
    for position, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        qty = _to_number(item.get("qty", 1), 1)
        price = _to_int(item.get("price"), 0)
        total = _to_int(item.get("total"), round(qty * price))
        items.append(
            {
                "id": _to_int(item.get("id"), position + 1),
                "name": str(item.get("name") or "").strip(),
                "qty": qty,
                "price": price,
                "total": total,
            }
        )

    subtotal = _to_number(
        raw.get("subtotal"), sum(item["total"] for item in items)
    )
    tax = _to_int(raw.get("tax"), 0)
    service_charge = _to_int(raw.get("service_charge"), 0)
    discounts = _to_int(raw.get("discounts"), 0)
    grand_total = _to_int(
        raw.get("grand_total"),
        round(subtotal + tax + service_charge - discounts),
    )

    return {
        "merchant_name": str(raw.get("merchant_name") or "").strip()
        or "Tagihan Tanpa Nama",
        "date": str(raw.get("date") or "").strip(),
        "items": items,
        "subtotal": subtotal,
        "tax": tax,
        "service_charge": service_charge,
        "discounts": discounts,
        "grand_total": grand_total,
    }


def _extract_token(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ApiError(400, "Masukkan link undangan terlebih dahulu.")

    if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", raw):
        return raw

    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    query = parse_qs(parsed.query)
    for key in ("token", "group", "g"):
        if key in query and query[key]:
            token = query[key][0].strip()
            if token:
                return token

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise ApiError(404, "Link undangan tidak valid atau tagihan tidak ditemukan.")
    return segments[-1]


def _find_member(data: dict, member_id: str) -> dict | None:
    return next(
        (member for member in data.get("members", []) if member["id"] == member_id),
        None,
    )


def _next_color_index(members: list[dict]) -> int:
    used = {int(member.get("color_index", 0)) for member in members}
    for index in range(len(MEMBER_COLORS)):
        if index not in used:
            return index
    return len(members) % len(MEMBER_COLORS)


def _new_member(name: str, role: str, color_index: int) -> dict:
    return {
        "id": f"m_{secrets.token_hex(5)}",
        "name": name,
        "role": role,
        "color_index": color_index,
    }


@app.errorhandler(ApiError)
def _handle_api_error(error: ApiError):
    return jsonify({"error": {"message": error.message}}), error.status


@app.after_request
def _add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = os.environ.get(
        "BILLIN_CORS_ORIGIN", "*"
    )
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, PUT, DELETE, OPTIONS"
    )
    return response


@app.route("/")
def index():
    return redirect(url_for("join"))


@app.route("/join")
@app.route("/join/<token>")
def join(token=None):
    return render_template("join.html", initial_token=token or "")


@app.route("/split/<token>")
def split(token: str):
    data = _load_group(token)
    if data is None:
        return redirect(url_for("join", token=token))
    return render_template(
        "index.html",
        token=token,
        initial_group=_group_payload(data),
    )


@app.route("/api/groups", methods=["POST"])
def create_group():
    payload = request.get_json(silent=True) or {}
    receipt = _normalize_receipt(payload.get("receipt"))
    host_name = _clean_name(payload.get("host_name") or "Host")

    group = None
    for _ in range(12):
        token = secrets.token_urlsafe(6)
        now = _now()
        host = _new_member(host_name, "host", 0)
        candidate = {
            "token": token,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "receipt": receipt,
            "members": [host],
            "assignments": {},
        }
        try:
            _insert_group(candidate)
            group = candidate
            break
        except sqlite3.IntegrityError:
            continue

    if group is None:
        raise ApiError(500, "Gagal membuat tagihan. Coba lagi.")

    return (
        jsonify(
            {
                "group": _group_payload(group),
                "member_id": group["members"][0]["id"],
            }
        ),
        201,
    )


@app.route("/api/groups/resolve", methods=["GET"])
def resolve_group():
    token = _extract_token(request.args.get("link"))
    data = _load_group(token)
    if data is None:
        raise ApiError(
            404,
            "Tagihan tidak ditemukan. Periksa kembali link undangan.",
        )
    return jsonify({"group": _group_payload(data)})


@app.route("/api/groups/<token>", methods=["GET"])
def get_group(token: str):
    data = _load_group(token)
    if data is None:
        raise ApiError(404, "Tagihan tidak ditemukan.")
    return jsonify({"group": _group_payload(data)})


@app.route("/api/groups/<token>/members", methods=["POST"])
def add_member(token: str):
    payload = request.get_json(silent=True) or {}
    name = _clean_name(payload.get("name"))

    def mutate(data: dict) -> dict:
        member = _new_member(
            name,
            "member",
            _next_color_index(data.get("members", [])),
        )
        data.setdefault("members", []).append(member)
        return member

    result = _mutate_group(token, mutate)
    if result is None:
        raise ApiError(404, "Tagihan tidak ditemukan.")

    data, member = result
    return (
        jsonify({"group": _group_payload(data), "member_id": member["id"]}),
        201,
    )


@app.route("/api/groups/<token>/members/<member_id>", methods=["DELETE"])
def remove_member(token: str, member_id: str):
    def mutate(data: dict) -> None:
        member = _find_member(data, member_id)
        if member is None:
            raise ApiError(404, "Anggota tidak ditemukan.")
        if member.get("role") == "host":
            raise ApiError(400, "Pembuat tagihan tidak dapat dihapus.")

        data["members"].remove(member)
        for key in list(data.get("assignments", {}).keys()):
            remaining = [
                assigned
                for assigned in data["assignments"][key]
                if assigned != member_id
            ]
            if remaining:
                data["assignments"][key] = remaining
            else:
                del data["assignments"][key]

    result = _mutate_group(token, mutate)
    if result is None:
        raise ApiError(404, "Tagihan tidak ditemukan.")

    data, _ = result
    return jsonify({"group": _group_payload(data)})


@app.route(
    "/api/groups/<token>/members/<member_id>/assignments",
    methods=["PUT"],
)
def update_assignments(token: str, member_id: str):
    payload = request.get_json(silent=True) or {}
    raw_indexes = payload.get("item_indexes")
    if not isinstance(raw_indexes, list):
        raise ApiError(400, "Daftar item yang dipilih tidak valid.")

    indexes = set()
    for value in raw_indexes:
        try:
            indexes.add(int(value))
        except (TypeError, ValueError):
            raise ApiError(400, "Daftar item yang dipilih tidak valid.")

    def mutate(data: dict) -> None:
        member = _find_member(data, member_id)
        if member is None:
            raise ApiError(404, "Anggota tidak ditemukan.")

        item_count = len(data.get("receipt", {}).get("items", []))
        for value in indexes:
            if value < 0 or value >= item_count:
                raise ApiError(400, "Item yang dipilih tidak valid.")

        assignments = data.setdefault("assignments", {})
        for key in list(assignments.keys()):
            remaining = [
                assigned
                for assigned in assignments[key]
                if assigned != member_id
            ]
            if remaining:
                assignments[key] = remaining
            else:
                del assignments[key]

        for value in sorted(indexes):
            key = str(value)
            assigned = assignments.setdefault(key, [])
            if member_id not in assigned:
                assigned.append(member_id)

    result = _mutate_group(token, mutate)
    if result is None:
        raise ApiError(404, "Tagihan tidak ditemukan.")

    data, _ = result
    return jsonify({"group": _group_payload(data)})


if __name__ == "__main__":
    server = Server(app.wsgi_app)
    server.watch("templates/*")
    server.watch("static/css/*")
    server.serve(
        host=os.environ.get("BILLIN_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
    )

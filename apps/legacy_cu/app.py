"""Riverton Heritage Credit Union — MemberLink 2003.

Local stand-in for a hostile legacy back office (SPEC §1). Markup is
table/iframe/ASP.NET-id ugly on purpose; labels and accessible names are
kept intact so the perception layer can read the accessibility tree.

Run:  python apps/legacy_cu/app.py
Open: http://127.0.0.1:5001/
"""

from __future__ import annotations

import copy
import os
import time
import uuid
from functools import wraps

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = os.environ.get("LEGACY_CU_SECRET", "legacy-cu-demo-not-secret")

DEMO_PASSWORD = "demo123"
SLOW_SECONDS = 6
INJECT_FLAGS = frozenset(
    {"not_found", "validation", "permission", "dialog", "timeout", "slow"}
)
SKIP_SLOW_ENDPOINTS = frozenset({"shell", "rates", "static"})

SEED_MEMBERS = {
    "100241": {
        "member_id": "100241",
        "name": "Eleanor Vasquez",
        "last_name": "Vasquez",
        "status": "Active",
        "dob": "1964-03-18",
        "phone": "(555) 014-2201",
        "email": "e.vasquez@example.net",
        "address": "14 Maple Court, Riverton, ST 00011",
        "savings_balance": 12847.55,
        "subaccounts": [
            {
                "share_id": "100241-S1",
                "share_type": "Regular Savings",
                "nickname": "Primary savings",
                "balance": 12847.55,
            },
            {
                "share_id": "100241-S2",
                "share_type": "Vacation Club",
                "nickname": "Beach fund",
                "balance": 640.00,
            },
        ],
    },
    "100387": {
        "member_id": "100387",
        "name": "Thomas Okonkwo",
        "last_name": "Okonkwo",
        "status": "Active",
        "dob": "1978-11-02",
        "phone": "(555) 014-8830",
        "email": "t.okonkwo@example.net",
        "address": "88 Harbor Road, Riverton, ST 00011",
        "savings_balance": 4102.00,
        "subaccounts": [
            {
                "share_id": "100387-S1",
                "share_type": "Regular Savings",
                "nickname": "Primary savings",
                "balance": 4102.00,
            }
        ],
    },
    "100512": {
        "member_id": "100512",
        "name": "Priya Nandakumar",
        "last_name": "Nandakumar",
        "status": "Active",
        "dob": "1991-07-29",
        "phone": "(555) 014-4419",
        "email": "p.nandakumar@example.net",
        "address": "2 Cedar Lane Apt 4B, Riverton, ST 00011",
        "savings_balance": 89330.18,
        "subaccounts": [
            {
                "share_id": "100512-S1",
                "share_type": "Regular Savings",
                "nickname": "Primary savings",
                "balance": 89330.18,
            },
            {
                "share_id": "100512-S2",
                "share_type": "IRA Share",
                "nickname": "Retirement",
                "balance": 22100.00,
            },
        ],
    },
}

SHARE_TYPES = (
    "Regular Savings",
    "Vacation Club",
    "Holiday Club",
    "IRA Share",
)

_STORES: dict[str, dict] = {}


def current_inject() -> str | None:
    raw = request.args.get("inject")
    if raw is None:
        raw = request.form.get("inject", "")
    raw = (raw or "").strip()
    return raw if raw in INJECT_FLAGS else None


def cu_url(endpoint: str, **values) -> str:
    inj = current_inject()
    if inj:
        values.setdefault("inject", inj)
    return url_for(endpoint, **values)


@app.context_processor
def _template_globals():
    return {
        "cu_url": cu_url,
        "inject": current_inject(),
        "teller": session.get("teller"),
        "share_types": SHARE_TYPES,
        "demo_password": DEMO_PASSWORD,
    }


def _store() -> dict:
    sid = session.setdefault("sid", uuid.uuid4().hex)
    if sid not in _STORES:
        _STORES[sid] = copy.deepcopy(SEED_MEMBERS)
    return _STORES[sid]


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_inject() == "timeout":
            session.pop("teller", None)
            return render_template(
                "login.html",
                error="Your session has timed out. Please sign in again. (ERR-440)",
                teller_id="",
            )
        if not session.get("teller"):
            return redirect(cu_url("login"))
        return view(*args, **kwargs)

    return wrapped


@app.before_request
def _apply_slow():
    if current_inject() == "slow" and request.endpoint not in SKIP_SLOW_ENDPOINTS:
        time.sleep(SLOW_SECONDS)


def _form(name: str, default: str = "") -> str:
    return (request.form.get(name) or default).strip()


def _find_members(member_id: str, last_name: str) -> list[dict]:
    store = _store()
    if member_id:
        record = store.get(member_id)
        return [record] if record else []
    needle = last_name.lower()
    return [
        m
        for m in store.values()
        if needle and needle in m["last_name"].lower()
    ]


def _member_or_none(member_id: str) -> dict | None:
    return _store().get(member_id)


# --- chrome (outer page + nested rates frame) ---------------------------------

@app.route("/")
def shell():
    iframe_src = cu_url("search") if session.get("teller") else cu_url("login")
    return render_template("shell.html", iframe_src=iframe_src)


@app.route("/frames/Rates.aspx")
def rates():
    return render_template("rates.html")


# --- screens ------------------------------------------------------------------

@app.route("/Login.aspx", methods=["GET", "POST"])
def login():
    if current_inject() == "timeout" and request.method == "GET":
        session.pop("teller", None)
        return render_template(
            "login.html",
            error="Your session has timed out. Please sign in again. (ERR-440)",
            teller_id="",
        )

    error = None
    teller_id = _form("ctl00$MainContent$txtTellerId")
    if request.method == "POST":
        password = request.form.get("ctl00$MainContent$txtPassword") or ""
        if not teller_id:
            error = "Teller ID is required."
        elif password != DEMO_PASSWORD:
            error = "Invalid logon. Please re-enter your password."
        else:
            session["teller"] = teller_id
            return redirect(cu_url("search"))
    elif session.get("teller"):
        return redirect(cu_url("search"))
    return render_template("login.html", error=error, teller_id=teller_id)


@app.route("/Logout.aspx")
def logout():
    session.pop("teller", None)
    return redirect(cu_url("login"))


@app.route("/MemberSearch.aspx", methods=["GET", "POST"])
@_login_required
def search():
    member_id = _form("ctl00$MainContent$txtMemberId")
    last_name = _form("ctl00$MainContent$txtLastName")
    results: list[dict] | None = None
    error = None
    searched = request.method == "POST"

    if searched:
        if current_inject() == "validation":
            error = "Member ID must be 6 numeric digits. (VAL-109)"
        elif not member_id and not last_name:
            error = "Enter a Member ID or last name."
        elif current_inject() == "not_found":
            results = []
        else:
            results = _find_members(member_id, last_name)

    return render_template(
        "search.html",
        member_id=member_id,
        last_name=last_name,
        results=results,
        error=error,
        searched=searched,
        money=_money,
    )


@app.route("/MemberDetail.aspx")
@_login_required
def member():
    member_id = (request.args.get("member_id") or "").strip()
    if current_inject() == "permission":
        return render_template("denied.html", member_id=member_id)
    if current_inject() == "not_found":
        return render_template("not_found.html", member_id=member_id)

    record = _member_or_none(member_id)
    if not record:
        return render_template("not_found.html", member_id=member_id)
    return render_template("member.html", member=record, money=_money)


@app.route("/NewShare.aspx", methods=["GET", "POST"])
@_login_required
def new_share():
    member_id = (
        _form("ctl00$MainContent$hidMemberId")
        or (request.args.get("member_id") or "").strip()
    )
    if current_inject() == "permission":
        return render_template("denied.html", member_id=member_id)
    if current_inject() == "not_found":
        return render_template("not_found.html", member_id=member_id)

    record = _member_or_none(member_id)
    if not record:
        return render_template("not_found.html", member_id=member_id)

    share_type = _form("ctl00$MainContent$ddlShareType", SHARE_TYPES[0])
    nickname = _form("ctl00$MainContent$txtNickname")
    deposit = _form("ctl00$MainContent$txtDeposit")
    errors: list[str] = []

    if request.method == "POST":
        if current_inject() == "validation":
            errors = [
                "Nickname contains invalid characters. (VAL-204)",
                "Initial deposit must be in increments of $25.00. (VAL-218)",
            ]
        else:
            if not nickname:
                errors.append("Nickname is required.")
            if share_type not in SHARE_TYPES:
                errors.append("Share type is required.")
            try:
                amount = float(deposit.replace(",", ""))
                if amount < 0:
                    errors.append("Initial deposit cannot be negative.")
            except ValueError:
                errors.append("Initial deposit must be a number.")
                amount = None
            if not errors and amount is not None:
                return render_template(
                    "confirm.html",
                    member=record,
                    share_type=share_type,
                    nickname=nickname,
                    deposit=f"{amount:.2f}",
                    deposit_display=_money(amount),
                    money=_money,
                )

    return render_template(
        "new_share.html",
        member=record,
        share_type=share_type,
        nickname=nickname,
        deposit=deposit,
        errors=errors,
        money=_money,
    )


@app.route("/ConfirmShare.aspx", methods=["POST"])
@_login_required
def confirm_share():
    member_id = _form("ctl00$MainContent$hidMemberId")
    if current_inject() == "permission":
        return render_template("denied.html", member_id=member_id)
    if current_inject() == "not_found":
        return render_template("not_found.html", member_id=member_id)
    if current_inject() == "validation":
        record = _member_or_none(member_id)
        return render_template(
            "new_share.html",
            member=record or {"member_id": member_id, "name": "", "savings_balance": 0},
            share_type=_form("ctl00$MainContent$ddlShareType"),
            nickname=_form("ctl00$MainContent$txtNickname"),
            deposit=_form("ctl00$MainContent$txtDeposit"),
            errors=[
                "Nickname contains invalid characters. (VAL-204)",
                "Initial deposit must be in increments of $25.00. (VAL-218)",
            ],
            money=_money,
        )

    record = _member_or_none(member_id)
    if not record:
        return render_template("not_found.html", member_id=member_id)

    share_type = _form("ctl00$MainContent$ddlShareType")
    nickname = _form("ctl00$MainContent$txtNickname")
    deposit_raw = _form("ctl00$MainContent$txtDeposit")
    try:
        amount = float(deposit_raw.replace(",", ""))
    except ValueError:
        amount = 0.0

    next_n = len(record["subaccounts"]) + 1
    record["subaccounts"].append(
        {
            "share_id": f"{member_id}-S{next_n}",
            "share_type": share_type or "Regular Savings",
            "nickname": nickname or "Untitled",
            "balance": amount,
        }
    )
    return redirect(cu_url("member", member_id=member_id))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)

from flask import Blueprint, abort, current_app, jsonify, request

from app.keys import ReplayError
from app.modules.m8_variance.service import CALIBERS
from app.web import record

api = Blueprint("variance", __name__)


def statement_store():
    return current_app.extensions["statements"]


def choose_caliber():
    caliber = request.args.get("caliber", "platform")
    if caliber not in CALIBERS:
        raise ReplayError("CALIBER", "口径仅支持 platform 或 source")
    return caliber


@api.get("/api/variance/<plan>")
def inspection(plan):
    result = record(plan)
    if not result.get("差额说明"):
        abort(404)
    caliber = choose_caliber()
    return jsonify({"口径": caliber, "差额说明": result["差额说明"]})


@api.post("/api/variance/<plan>/issue")
def issue(plan):
    result = record(plan)
    if not result.get("差额说明"):
        abort(404)
    statement, created = statement_store().issue(result["差额说明"])
    return jsonify({"说明编号": statement["说明编号"], "created": created, "reused": not created}), (
        201 if created else 200
    )


@api.get("/api/variance/statements")
def statements():
    return jsonify(statement_store().history())


@api.get("/api/variance/audit")
def audit():
    return jsonify(statement_store().audit_trail())


# 说明账本只追加；显式拒绝任何删除/改写尝试，旧说明永不丢失。
@api.delete("/api/variance/statement/<statement_id>")
@api.patch("/api/variance/statement/<statement_id>")
@api.put("/api/variance/statement/<statement_id>")
def immutable(statement_id):
    return jsonify(code="IMMUTABLE", message="差额说明只追加，禁止删除或改写"), 405

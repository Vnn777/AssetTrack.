import csv
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "local-dev-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'assettrack.db'}").replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE") == "1"
(BASE_DIR / "instance").mkdir(exist_ok=True)
db = SQLAlchemy(app)
STATUSES = ["Disponível", "Em uso", "Em manutenção", "Baixado"]
CATEGORIES = ["Notebook", "Computador", "Monitor", "Impressora", "Celular", "Periférico", "Rede", "Outro"]

class Asset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tag = db.Column(db.String(60), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(40), nullable=False)
    brand = db.Column(db.String(80), default="")
    model = db.Column(db.String(100), default="")
    serial = db.Column(db.String(120), default="")
    sector = db.Column(db.String(100), default="")
    assignee = db.Column(db.String(100), default="")
    status = db.Column(db.String(30), nullable=False, default="Disponível")
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    movements = db.relationship("Movement", backref="asset", cascade="all, delete-orphan", order_by="Movement.id.desc()")

class Movement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("asset.id"), nullable=False)
    kind = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

with app.app_context():
    db.create_all()

@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response

def clean(value, limit=120):
    return str(value or "").strip()[:limit]

def fields_from_form():
    data = {k: clean(request.form.get(k), limit) for k, limit in {
        "tag":60,"name":120,"category":40,"brand":80,"model":100,
        "serial":120,"sector":100,"assignee":100,"status":30,"notes":2000
    }.items()}
    if not data["tag"] or not data["name"]:
        raise ValueError("Informe o patrimônio e o nome do equipamento.")
    if data["category"] not in CATEGORIES or data["status"] not in STATUSES:
        raise ValueError("Categoria ou status inválido.")
    return data

@app.get("/")
def index():
    query = clean(request.args.get("q"), 120)
    status = clean(request.args.get("status"), 30)
    category = clean(request.args.get("category"), 40)
    stmt = Asset.query
    if query:
        term = f"%{query}%"
        stmt = stmt.filter(or_(Asset.tag.ilike(term), Asset.name.ilike(term), Asset.assignee.ilike(term), Asset.serial.ilike(term), Asset.sector.ilike(term)))
    if status in STATUSES:
        stmt = stmt.filter(Asset.status == status)
    if category in CATEGORIES:
        stmt = stmt.filter(Asset.category == category)
    assets = stmt.order_by(Asset.id.desc()).all()
    counts = {"total": Asset.query.count(), **{s: Asset.query.filter_by(status=s).count() for s in STATUSES}}
    recent = Movement.query.order_by(Movement.id.desc()).limit(6).all()
    return render_template("index.html", assets=assets, counts=counts, recent=recent, statuses=STATUSES, categories=CATEGORIES, q=query, selected_status=status, selected_category=category)

@app.post("/assets")
def create_asset():
    try:
        asset = Asset(**fields_from_form())
        db.session.add(asset)
        db.session.flush()
        db.session.add(Movement(asset_id=asset.id, kind="Cadastro", detail="Equipamento cadastrado no inventário."))
        db.session.commit()
        flash("Equipamento cadastrado com sucesso.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except IntegrityError:
        db.session.rollback()
        flash("Esse número de patrimônio já está cadastrado.", "error")
    return redirect(url_for("index"))

@app.post("/assets/<int:asset_id>/edit")
def edit_asset(asset_id):
    asset = db.get_or_404(Asset, asset_id)
    try:
        data = fields_from_form()
        changes = [f"{k}: {getattr(asset,k) or '—'} → {v or '—'}" for k,v in data.items() if str(getattr(asset,k) or "") != v and k != "notes"]
        for key,value in data.items():
            setattr(asset,key,value)
        if changes:
            db.session.add(Movement(asset_id=asset.id,kind="Atualização",detail="; ".join(changes)[:500]))
        db.session.commit()
        flash("Equipamento atualizado.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except IntegrityError:
        db.session.rollback()
        flash("Esse número de patrimônio já pertence a outro equipamento.", "error")
    return redirect(url_for("index"))

@app.post("/assets/<int:asset_id>/movement")
def movement(asset_id):
    asset = db.get_or_404(Asset,asset_id)
    kind = clean(request.form.get("kind"),40)
    detail = clean(request.form.get("detail"),500)
    if kind not in ["Entrega","Devolução","Transferência","Manutenção","Observação"] or not detail:
        flash("Selecione o tipo e descreva a movimentação.","error")
    else:
        db.session.add(Movement(asset_id=asset.id,kind=kind,detail=detail))
        db.session.commit()
        flash("Movimentação registrada.","success")
    return redirect(url_for("index"))

@app.get("/assets/<int:asset_id>/history")
def history(asset_id):
    asset = db.get_or_404(Asset,asset_id)
    return jsonify([{"kind":m.kind,"detail":m.detail,"date":m.created_at.strftime("%d/%m/%Y %H:%M")} for m in asset.movements])

@app.get("/export.csv")
def export_csv():
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Patrimônio","Equipamento","Categoria","Marca","Modelo","Série","Setor","Responsável","Status","Observações"])
    for a in Asset.query.order_by(Asset.tag).all():
        writer.writerow([a.tag,a.name,a.category,a.brand,a.model,a.serial,a.sector,a.assignee,a.status,a.notes])
    return Response("\ufeff"+buffer.getvalue(),mimetype="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=assettrack_inventario.csv"})

@app.get("/health")
def health():
    return {"status":"ok"}

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")

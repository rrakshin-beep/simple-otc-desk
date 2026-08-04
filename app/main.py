from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from uuid import uuid4
from zoneinfo import ZoneInfo
import csv

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from .database import Base, engine, get_db
from .models import AuditLog, AmountType, Quote, QuoteAcceptance, RFQ, RFQStatus, Trade, TradeHistory, TradeStatus, Party, PartyType, ReportingProfile, TradeReporting
from .reference_data import get_reference, reference_context

Base.metadata.create_all(bind=engine)

from .regulatory import CURRENCY_CODES, generate_xml, validate_report


def migrate_sqlite_schema() -> None:
    additions = {
        "rfqs": {
            "fiat_amount": "NUMERIC(30, 8)", "amount_type": "VARCHAR(16) DEFAULT 'CRYPTO'",
            "network": "VARCHAR(40)", "rejected_at": "DATETIME", "rejected_by": "VARCHAR(120)",
            "rejection_reason": "TEXT",
        },
        "parties": {"party_type_code": "VARCHAR(3) DEFAULT '002'"},
        "trade_reporting": {"client_participant_kind": "VARCHAR(2) DEFAULT '05'", "exchange_participant_kind": "VARCHAR(2) DEFAULT '04'"},
        "trades": {
            "bank_fee": "NUMERIC(30, 8) DEFAULT 0", "network_fee": "NUMERIC(30, 8) DEFAULT 0",
            "bank_fee_payer": "VARCHAR(20) DEFAULT 'CLIENT'", "network_fee_payer": "VARCHAR(20) DEFAULT 'CLIENT'",
            "fees_included_in_quote": "BOOLEAN DEFAULT 0", "bank_reference": "VARCHAR(120)",
            "tx_hash": "VARCHAR(180)", "aml_risk": "VARCHAR(20)", "cancellation_reason": "TEXT",
            "archived": "BOOLEAN DEFAULT 0", "archived_at": "DATETIME", "archived_by": "VARCHAR(120)",
            "archive_reason": "TEXT",
        },
    }
    inspector = inspect(engine)
    for table_name, fields in additions.items():
        if table_name not in inspector.get_table_names():
            continue
        columns = {c["name"] for c in inspector.get_columns(table_name)}
        with engine.begin() as connection:
            for name, ddl in fields.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))


migrate_sqlite_schema()
app = FastAPI(title="Simple OTC Desk")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
BISHKEK = ZoneInfo("Asia/Bishkek")
CRYPTO_ASSETS = ["USDT", "BTC", "USDC", "TRX", "ETH", "TON"]
FIAT_ASSETS = ["KGS", "USD", "EUR", "RUB"]
NETWORKS = ["TRC-20", "ERC-20", "TON", "BEP-20", "Bitcoin", "Ethereum", "Tron"]
SIDE_LABELS = {"BUY": "Покупка", "SELL": "Продажа"}
STATUS_LABELS = {
    "SUBMITTED": "Запрос создан", "QUOTED": "Котировка выставлена", "ACCEPTED": "Принято",
    "REJECTED": "Отклонено", "EXPIRED": "Срок истек", "CANCELLED": "Отменено",
    "FUNDED": "Средства получены", "AML_REVIEW": "AML-проверка", "APPROVED": "Одобрено",
    "EXECUTED": "Исполнено", "SETTLED": "Расчеты завершены", "COMPLETED": "Завершено",
}
ALLOWED_TRANSITIONS = {
    TradeStatus.ACCEPTED: {TradeStatus.FUNDED, TradeStatus.CANCELLED},
    TradeStatus.FUNDED: {TradeStatus.AML_REVIEW, TradeStatus.APPROVED, TradeStatus.CANCELLED},
    TradeStatus.AML_REVIEW: {TradeStatus.APPROVED, TradeStatus.CANCELLED},
    TradeStatus.APPROVED: {TradeStatus.EXECUTED, TradeStatus.CANCELLED},
    TradeStatus.EXECUTED: {TradeStatus.SETTLED}, TradeStatus.SETTLED: {TradeStatus.COMPLETED},
    TradeStatus.COMPLETED: set(), TradeStatus.CANCELLED: set(),
}
REPORT_STATUS_COLUMNS = list(TradeStatus)


def utcnow() -> datetime:
    return datetime.utcnow()


def format_datetime(value: datetime | None) -> str:
    if not value:
        return "—"
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.astimezone(BISHKEK).strftime("%d.%m.%Y %H:%M:%S UTC+6")


def label(value) -> str:
    raw = value.value if hasattr(value, "value") else str(value)
    return STATUS_LABELS.get(raw, SIDE_LABELS.get(raw, raw))


templates.env.globals.update(format_datetime=format_datetime, label=label)


def money(value) -> Decimal:
    return Decimal(str(value or 0))


def audit(db: Session, entity_type: str, entity_id: int, action: str, actor: str, details: str = "") -> None:
    db.add(AuditLog(entity_type=entity_type, entity_id=entity_id, action=action, actor=actor or "system", details=details))


def expire_quotes(db: Session) -> None:
    expired = db.query(RFQ).join(Quote).filter(RFQ.status == RFQStatus.QUOTED, Quote.expires_at < utcnow()).all()
    for rfq in expired:
        rfq.status = RFQStatus.EXPIRED
        audit(db, "RFQ", rfq.id, "QUOTE_EXPIRED", "system")
    if expired:
        db.commit()


def calculate_amounts(rfq: RFQ, price: Decimal) -> tuple[Decimal, Decimal]:
    if price <= 0:
        raise HTTPException(400, "Цена должна быть больше нуля")
    if rfq.amount_type == AmountType.FIAT:
        fiat_amount = money(rfq.fiat_amount)
        if fiat_amount <= 0:
            raise HTTPException(400, "Сумма фиата должна быть больше нуля")
        return fiat_amount / price, fiat_amount
    crypto_amount = money(rfq.amount)
    if crypto_amount <= 0:
        raise HTTPException(400, "Количество криптовалюты должно быть больше нуля")
    return crypto_amount, crypto_amount * price


def status_timestamps(trade: Trade) -> dict[str, str]:
    timestamps = {}
    for item in sorted(trade.history, key=lambda x: (x.created_at, x.id)):
        timestamps.setdefault(item.new_status, item.created_at)
    timestamps.setdefault(TradeStatus.ACCEPTED.value, trade.created_at)
    return {s.value: format_datetime(timestamps.get(s.value)) for s in REPORT_STATUS_COLUMNS}


def current_status_time(trade: Trade) -> str:
    matching = [h for h in trade.history if h.new_status == trade.status.value]
    return format_datetime(max((h.created_at for h in matching), default=trade.created_at))


def trade_values(trade: Trade) -> dict:
    rfq = trade.quote.rfq
    return {
        "crypto_amount": money(rfq.amount), "fiat_amount": money(rfq.fiat_amount),
        "bank_fee": money(trade.bank_fee), "network_fee": money(trade.network_fee),
        "status_times": status_timestamps(trade), "current_status_time": current_status_time(trade),
    }


def dashboard_context(db: Session, role: str):
    expire_quotes(db)
    rfqs = db.query(RFQ).options(joinedload(RFQ.quote)).order_by(RFQ.id.desc()).all()
    trades = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq)).filter(Trade.archived.is_(False)).order_by(Trade.id.desc()).all()
    return {"rfqs": rfqs, "trades": trades, "role": role, "crypto_assets": CRYPTO_ASSETS,
            "fiat_assets": FIAT_ASSETS, "networks": NETWORKS, "accept_key": str(uuid4())}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="index.html", context=dashboard_context(db, "dealer"))


@app.get("/client", response_class=HTMLResponse)
def client_dashboard(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="index.html", context=dashboard_context(db, "client"))


@app.get("/dealer", response_class=HTMLResponse)
def dealer_dashboard(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="index.html", context=dashboard_context(db, "dealer"))


@app.get("/compliance", response_class=HTMLResponse)
def compliance_dashboard(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/regulatory", 302)


@app.post("/rfqs")
def create_rfq(client_name: str = Form(...), side: str = Form(...), base_asset: str = Form(...),
               quote_asset: str = Form(...), network: str = Form(""), amount_type: AmountType = Form(...),
               crypto_amount: str = Form(""), fiat_amount: str = Form(""), comment: str = Form(""),
               return_to: str = Form("/client"), db: Session = Depends(get_db)):
    if base_asset not in CRYPTO_ASSETS or quote_asset not in FIAT_ASSETS:
        raise HTTPException(400, "Выберите валюту из перечня")
    try:
        crypto = Decimal(crypto_amount) if crypto_amount.strip() else None
        fiat = Decimal(fiat_amount) if fiat_amount.strip() else None
    except InvalidOperation as exc:
        raise HTTPException(400, "Количество должно быть числом") from exc
    if amount_type == AmountType.CRYPTO and (crypto is None or crypto <= 0):
        raise HTTPException(400, "Укажите количество криптовалюты")
    if amount_type == AmountType.FIAT and (fiat is None or fiat <= 0):
        raise HTTPException(400, "Укажите сумму фиата")
    rfq = RFQ(client_name=client_name.strip(), side=side, base_asset=base_asset, quote_asset=quote_asset,
              network=network or None, amount_type=amount_type, amount=crypto if amount_type == AmountType.CRYPTO else None,
              fiat_amount=fiat if amount_type == AmountType.FIAT else None, comment=comment.strip())
    db.add(rfq); db.flush(); audit(db, "RFQ", rfq.id, "CREATED", client_name, comment); db.commit()
    redirect_target = "/dealer" if return_to == "/dealer" else "/client"
    return RedirectResponse(redirect_target, 303)


@app.post("/rfqs/{rfq_id}/quote")
def create_quote(rfq_id: int, price: Decimal = Form(...), valid_minutes: int = Form(5),
                 dealer_name: str = Form(...), db: Session = Depends(get_db)):
    rfq = db.get(RFQ, rfq_id)
    if not rfq or rfq.status != RFQStatus.SUBMITTED:
        raise HTTPException(400, "RFQ недоступен для котирования")
    crypto, fiat = calculate_amounts(rfq, price)
    rfq.amount, rfq.fiat_amount, rfq.status = crypto, fiat, RFQStatus.QUOTED
    quote = Quote(rfq=rfq, price=price, expires_at=utcnow() + timedelta(minutes=max(valid_minutes, 1)), dealer_name=dealer_name.strip())
    db.add(quote); db.flush(); audit(db, "RFQ", rfq.id, "QUOTED", dealer_name, f"Цена {price}"); db.commit()
    return RedirectResponse("/dealer", 303)


@app.post("/quotes/{quote_id}/reject")
def reject_quote(quote_id: int, rejected_by: str = Form(...), reason: str = Form(...), db: Session = Depends(get_db)):
    quote = db.get(Quote, quote_id)
    if not quote or quote.rfq.status != RFQStatus.QUOTED:
        raise HTTPException(400, "Котировка недоступна для отклонения")
    quote.rfq.status, quote.rfq.rejected_at = RFQStatus.REJECTED, utcnow()
    quote.rfq.rejected_by, quote.rfq.rejection_reason = rejected_by.strip(), reason.strip()
    audit(db, "RFQ", quote.rfq.id, "QUOTE_REJECTED", rejected_by, reason); db.commit()
    return RedirectResponse("/client", 303)


@app.post("/quotes/{quote_id}/accept")
def accept_quote(quote_id: int, idempotency_key: str = Form(...), confirm: str = Form(...), db: Session = Depends(get_db)):
    if confirm != "yes":
        raise HTTPException(400, "Необходимо подтвердить условия")
    existing = db.query(QuoteAcceptance).filter(QuoteAcceptance.idempotency_key == idempotency_key).first()
    if existing:
        trade = db.query(Trade).filter(Trade.quote_id == existing.quote_id).first()
        return RedirectResponse(f"/trades/{trade.id}", 303) if trade else RedirectResponse("/client", 303)
    quote = db.query(Quote).options(joinedload(Quote.rfq), joinedload(Quote.trade)).filter(Quote.id == quote_id).with_for_update().first()
    if not quote: raise HTTPException(404, "Котировка не найдена")
    if quote.trade or quote.rfq.status == RFQStatus.ACCEPTED: raise HTTPException(409, "Котировка уже принята")
    if quote.rfq.status != RFQStatus.QUOTED: raise HTTPException(400, "Котировка недоступна")
    if quote.expires_at < utcnow():
        quote.rfq.status = RFQStatus.EXPIRED; db.commit(); raise HTTPException(400, "Срок котировки истек")
    quote.rfq.status = RFQStatus.ACCEPTED
    trade = Trade(quote=quote, status=TradeStatus.ACCEPTED)
    db.add_all([trade, QuoteAcceptance(quote_id=quote.id, idempotency_key=idempotency_key)])
    try:
        db.flush(); db.add(TradeHistory(trade_id=trade.id, old_status=None, new_status="ACCEPTED", changed_by=quote.rfq.client_name, note="Котировка принята клиентом")); audit(db, "TRADE", trade.id, "CREATED", quote.rfq.client_name); db.commit()
    except IntegrityError as exc:
        db.rollback(); raise HTTPException(409, "Котировка уже принята") from exc
    return RedirectResponse(f"/trades/{trade.id}", 303)


@app.post("/trades/{trade_id}/details")
def update_trade_details(trade_id: int, bank_fee: Decimal = Form(0), network_fee: Decimal = Form(0),
                         bank_fee_payer: str = Form("CLIENT"), network_fee_payer: str = Form("CLIENT"),
                         fees_included_in_quote: bool = Form(False), bank_reference: str = Form(""),
                         tx_hash: str = Form(""), aml_risk: str = Form(""), actor: str = Form("OTC Operator"),
                         db: Session = Depends(get_db)):
    trade = db.get(Trade, trade_id)
    if not trade or trade.archived: raise HTTPException(404, "Сделка не найдена")
    if bank_fee < 0 or network_fee < 0: raise HTTPException(400, "Комиссии не могут быть отрицательными")
    trade.bank_fee, trade.network_fee = bank_fee, network_fee
    trade.bank_fee_payer, trade.network_fee_payer = bank_fee_payer, network_fee_payer
    trade.fees_included_in_quote, trade.bank_reference, trade.tx_hash, trade.aml_risk = fees_included_in_quote, bank_reference.strip(), tx_hash.strip(), aml_risk.strip()
    audit(db, "TRADE", trade.id, "DETAILS_UPDATED", actor); db.commit()
    return RedirectResponse(f"/trades/{trade_id}", 303)


@app.post("/trades/{trade_id}/status")
def change_status(trade_id: int, new_status: TradeStatus = Form(...), changed_by: str = Form(...),
                  note: str = Form(""), db: Session = Depends(get_db)):
    trade = db.get(Trade, trade_id)
    if not trade or trade.archived: raise HTTPException(404, "Сделка не найдена")
    if new_status not in ALLOWED_TRANSITIONS[trade.status]: raise HTTPException(400, "Запрещенный переход статуса")
    if new_status == TradeStatus.CANCELLED and not note.strip(): raise HTTPException(400, "Укажите причину отмены")
    old = trade.status; trade.status = new_status
    if new_status == TradeStatus.CANCELLED: trade.cancellation_reason = note.strip()
    db.add(TradeHistory(trade_id=trade.id, old_status=old.value, new_status=new_status.value, changed_by=changed_by.strip(), note=note.strip()))
    audit(db, "TRADE", trade.id, "STATUS_CHANGED", changed_by, f"{old.value}->{new_status.value}: {note}"); db.commit()
    return RedirectResponse(f"/trades/{trade_id}", 303)


@app.post("/trades/{trade_id}/archive")
def archive_trade(trade_id: int, archived_by: str = Form(...), reason: str = Form(...), db: Session = Depends(get_db)):
    trade = db.get(Trade, trade_id)
    if not trade: raise HTTPException(404, "Сделка не найдена")
    if not reason.strip(): raise HTTPException(400, "Укажите причину архивирования")
    trade.archived, trade.archived_at, trade.archived_by, trade.archive_reason = True, utcnow(), archived_by.strip(), reason.strip()
    audit(db, "TRADE", trade.id, "ARCHIVED", archived_by, reason); db.commit()
    return RedirectResponse("/dealer", 303)


@app.get("/trades/{trade_id}", response_class=HTMLResponse)
def trade_page(trade_id: int, request: Request, db: Session = Depends(get_db)):
    trade = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq), joinedload(Trade.history)).filter(Trade.id == trade_id).first()
    if not trade: raise HTTPException(404, "Сделка не найдена")
    return templates.TemplateResponse(request=request, name="trade.html", context={"trade": trade, "allowed": sorted(ALLOWED_TRANSITIONS[trade.status], key=lambda x: x.value), **trade_values(trade)})


def filtered_trades(db: Session, client: str = "", status: str = "", side: str = "", base_asset: str = "", quote_asset: str = "", include_archived: bool = False):
    q = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq), selectinload(Trade.history))
    if not include_archived: q = q.filter(Trade.archived.is_(False))
    if client: q = q.join(Quote).join(RFQ).filter(RFQ.client_name.contains(client))
    if status: q = q.filter(Trade.status == TradeStatus(status))
    if side: q = q.join(Quote, Trade.quote_id == Quote.id).join(RFQ, Quote.rfq_id == RFQ.id).filter(RFQ.side == side)
    if base_asset: q = q.join(Quote, Trade.quote_id == Quote.id).join(RFQ, Quote.rfq_id == RFQ.id).filter(RFQ.base_asset == base_asset)
    if quote_asset: q = q.join(Quote, Trade.quote_id == Quote.id).join(RFQ, Quote.rfq_id == RFQ.id).filter(RFQ.quote_asset == quote_asset)
    return q.order_by(Trade.id.desc()).all()


@app.get("/reports/trades", response_class=HTMLResponse)
def trades_report(request: Request, client: str = Query(""), status: str = Query(""), side: str = Query(""), base_asset: str = Query(""), quote_asset: str = Query(""), db: Session = Depends(get_db)):
    trades = filtered_trades(db, client, status, side, base_asset, quote_asset)
    rows = [{"trade": t, **trade_values(t)} for t in trades]
    return templates.TemplateResponse(request=request, name="report.html", context={"rows": rows, "status_columns": REPORT_STATUS_COLUMNS, "filters": locals(), "crypto_assets": CRYPTO_ASSETS, "fiat_assets": FIAT_ASSETS})


def report_matrix(trades):
    headers = ["Trade ID", "RFQ ID", "Quote ID", "Создано", "Клиент", "Операция", "Пара", "Сеть", "Количество криптовалюты", "Цена", "Сумма сделки", "Комиссия банка", "Валюта комиссии банка", "Плательщик комиссии банка", "Комиссия сети", "Валюта комиссии сети", "Плательщик комиссии сети", "Комиссии включены", "Текущий статус", "Дата текущего статуса", "Дилер", "AML-риск", "Банковский референс", "TX hash", "Причина отмены", "Последнее изменение"] + [f"Статус {label(s)}" for s in REPORT_STATUS_COLUMNS]
    rows = []
    for t in trades:
        r, v = t.quote.rfq, trade_values(t)
        rows.append([t.id, r.id, t.quote.id, format_datetime(t.created_at), r.client_name, label(r.side), f"{r.base_asset}/{r.quote_asset}", r.network or "—", v["crypto_amount"], t.quote.price, v["fiat_amount"], v["bank_fee"], r.quote_asset, t.bank_fee_payer, v["network_fee"], r.base_asset, t.network_fee_payer, "Да" if t.fees_included_in_quote else "Нет", label(t.status), v["current_status_time"], t.quote.dealer_name, t.aml_risk or "—", t.bank_reference or "—", t.tx_hash or "—", t.cancellation_reason or "—", format_datetime(t.updated_at)] + [v["status_times"][s.value] for s in REPORT_STATUS_COLUMNS])
    return headers, rows


@app.get("/reports/trades.csv")
def trades_report_csv(db: Session = Depends(get_db)):
    headers, rows = report_matrix(filtered_trades(db)); buffer = StringIO(); writer = csv.writer(buffer, delimiter=";"); writer.writerow(headers); writer.writerows(rows)
    return StreamingResponse(iter(["\ufeff" + buffer.getvalue()]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=otc_trades_report.csv"})


@app.get("/reports/trades.xlsx")
def trades_report_xlsx(db: Session = Depends(get_db)):
    headers, rows = report_matrix(filtered_trades(db)); wb = Workbook(); ws = wb.active; ws.title = "OTC сделки"; ws.append(headers)
    for row in rows: ws.append(row)
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    for col in ws.columns: ws.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col) + 2, 45)
    output = BytesIO(); wb.save(output); output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=otc_trades_report.xlsx"})


@app.get("/api/trades")
def api_trades(db: Session = Depends(get_db)):
    result = []
    for t in filtered_trades(db):
        r, v = t.quote.rfq, trade_values(t)
        result.append({"id": t.id, "status": t.status.value, "status_label": label(t.status), "client": r.client_name, "side": r.side, "pair": f"{r.base_asset}/{r.quote_asset}", "network": r.network, "crypto_amount": float(v["crypto_amount"]), "price": float(t.quote.price), "fiat_amount": float(v["fiat_amount"]), "bank_fee": float(v["bank_fee"]), "bank_fee_currency": r.quote_asset, "network_fee": float(v["network_fee"]), "network_fee_currency": r.base_asset})
    return result

@app.get("/parties", response_class=HTMLResponse)
def parties_page(request: Request, edit: int | None = Query(None), db: Session = Depends(get_db)):
    parties = db.query(Party).order_by(Party.id.desc()).all()
    edited = db.get(Party, edit) if edit else None
    return templates.TemplateResponse(request=request, name="parties.html", context={"parties": parties, "edited": edited, **reference_context()})


def _parse_optional_date(value: str):
    if not value or value == "00":
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "Неверный формат даты") from exc


def _apply_party_form(party: Party, *, party_type: PartyType, display_name: str, inn: str, okpo: str, country_code: str, resident_code: str, orgform_code: str, registration_number: str, registration_authority: str, activity: str, last_name: str, first_name: str, middle_name: str, document_code: str, document_series: str, document_number: str, document_issue_date: str, document_issuer: str, birth_date: str, birth_place: str, legal_postcode: str, legal_town_code: str, legal_region: str, legal_area: str, legal_town: str, legal_street: str, legal_house: str, legal_room: str, account_number: str, account_bank: str, account_bic: str, account_country_code: str, account_address: str):
    def clean(v): return (v or "").strip() or "00"
    party.party_type = party_type
    party.party_type_code = "002" if party_type == PartyType.LEGAL else "003"
    party.display_name = display_name.strip()
    party.inn, party.okpo = clean(inn), clean(okpo)
    party.country_code, party.resident_code = clean(country_code), resident_code
    party.orgform_code = clean(orgform_code) if party_type == PartyType.LEGAL else "00"
    party.registration_number, party.registration_authority, party.activity = clean(registration_number), clean(registration_authority), clean(activity)
    party.last_name, party.first_name, party.middle_name = clean(last_name), clean(first_name), clean(middle_name)
    party.document_code, party.document_series, party.document_number = clean(document_code), clean(document_series), clean(document_number)
    party.document_issue_date, party.document_issuer = _parse_optional_date(document_issue_date), clean(document_issuer)
    party.birth_date, party.birth_place = _parse_optional_date(birth_date), clean(birth_place)
    party.legal_postcode, party.legal_town_code = clean(legal_postcode), clean(legal_town_code)
    party.legal_region, party.legal_area, party.legal_town = clean(legal_region), clean(legal_area), clean(legal_town)
    party.legal_street, party.legal_house, party.legal_room = clean(legal_street), clean(legal_house), clean(legal_room)
    party.actual_postcode, party.actual_town_code = party.legal_postcode, party.legal_town_code
    party.actual_region, party.actual_area, party.actual_town = party.legal_region, party.legal_area, party.legal_town
    party.actual_street, party.actual_house, party.actual_room = party.legal_street, party.legal_house, party.legal_room
    party.account_number, party.account_bank, party.account_bic = clean(account_number), clean(account_bank), clean(account_bic)
    party.account_country_code, party.account_address = clean(account_country_code), clean(account_address)


@app.post("/parties")
def create_party(
    party_type: PartyType = Form(...), display_name: str = Form(...), inn: str = Form("00"), okpo: str = Form("00"), country_code: str = Form("417"), resident_code: str = Form("1"), orgform_code: str = Form("20"), registration_number: str = Form("00"), registration_authority: str = Form("00"), activity: str = Form("00"), last_name: str = Form("00"), first_name: str = Form("00"), middle_name: str = Form("00"), document_code: str = Form("00"), document_series: str = Form("00"), document_number: str = Form("00"), document_issue_date: str = Form(""), document_issuer: str = Form("00"), birth_date: str = Form(""), birth_place: str = Form("00"), legal_postcode: str = Form("00"), legal_town_code: str = Form("00"), legal_region: str = Form("00"), legal_area: str = Form("00"), legal_town: str = Form("00"), legal_street: str = Form("00"), legal_house: str = Form("00"), legal_room: str = Form("00"), account_number: str = Form("00"), account_bank: str = Form("00"), account_bic: str = Form("00"), account_country_code: str = Form("00"), account_address: str = Form("00"), db: Session = Depends(get_db)
):
    party = Party(party_type=party_type, display_name=display_name.strip())
    _apply_party_form(party, **{k:v for k,v in locals().items() if k not in {"party","db"}})
    db.add(party); db.commit()
    return RedirectResponse("/parties", 303)


@app.post("/parties/{party_id}")
def update_party(party_id: int, party_type: PartyType = Form(...), display_name: str = Form(...), inn: str = Form("00"), okpo: str = Form("00"), country_code: str = Form("417"), resident_code: str = Form("1"), orgform_code: str = Form("20"), registration_number: str = Form("00"), registration_authority: str = Form("00"), activity: str = Form("00"), last_name: str = Form("00"), first_name: str = Form("00"), middle_name: str = Form("00"), document_code: str = Form("00"), document_series: str = Form("00"), document_number: str = Form("00"), document_issue_date: str = Form(""), document_issuer: str = Form("00"), birth_date: str = Form(""), birth_place: str = Form("00"), legal_postcode: str = Form("00"), legal_town_code: str = Form("00"), legal_region: str = Form("00"), legal_area: str = Form("00"), legal_town: str = Form("00"), legal_street: str = Form("00"), legal_house: str = Form("00"), legal_room: str = Form("00"), account_number: str = Form("00"), account_bank: str = Form("00"), account_bic: str = Form("00"), account_country_code: str = Form("00"), account_address: str = Form("00"), db: Session = Depends(get_db)):
    party = db.get(Party, party_id)
    if not party: raise HTTPException(404, "Контрагент не найден")
    _apply_party_form(party, **{k:v for k,v in locals().items() if k not in {"party","party_id","db"}})
    db.commit()
    return RedirectResponse("/parties", 303)


@app.get("/settings/reporting", response_class=HTMLResponse)
def reporting_settings(request: Request, db: Session = Depends(get_db)):
    profile = db.query(ReportingProfile).first()
    return templates.TemplateResponse(request=request, name="reporting_settings.html", context={"profile": profile, **reference_context()})


@app.post("/settings/reporting")
def save_reporting_settings(
    inn: str = Form(...), person_name: str = Form(...), org_kind: str = Form("28"), bank_bic: str = Form("00"),
    okpo: str = Form("00"), orgform_code: str = Form("20"), branch: str = Form("00"),
    legal_postcode: str = Form("00"), legal_town_code: str = Form("00"), legal_region: str = Form("00"),
    legal_area: str = Form("00"), legal_town: str = Form("00"), legal_street: str = Form("00"),
    legal_house: str = Form("00"), legal_room: str = Form("00"), performer_name: str = Form("00"),
    performer_post: str = Form("00"), phone: str = Form("00"), db: Session = Depends(get_db)
):
    profile = db.query(ReportingProfile).first() or ReportingProfile(inn=inn, person_name=person_name)
    for key, value in locals().copy().items():
        if key in {"db", "profile"}: continue
        if hasattr(profile, key): setattr(profile, key, (value.strip() or "00") if isinstance(value, str) else value)
    db.add(profile); db.commit()
    return RedirectResponse("/settings/reporting", 303)


@app.get("/settings/reference-data", response_class=HTMLResponse)
def reference_data_page(request: Request):
    refs = reference_context()
    counts = {name: len(items) for name, items in refs.items()}
    return templates.TemplateResponse(request=request, name="reference_data.html", context={"counts": counts})


@app.get("/regulatory", response_class=HTMLResponse)
def regulatory_dashboard(request: Request, db: Session = Depends(get_db)):
    reports = db.query(TradeReporting).options(joinedload(TradeReporting.trade).joinedload(Trade.quote).joinedload(Quote.rfq)).order_by(TradeReporting.id.desc()).all()
    return templates.TemplateResponse(request=request, name="regulatory.html", context={"reports": reports})


@app.get("/trades/{trade_id}/form1", response_class=HTMLResponse)
@app.get("/trades/{trade_id}/regulatory", response_class=HTMLResponse)
def regulatory_trade_page(trade_id: int, request: Request, db: Session = Depends(get_db)):
    trade = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq)).filter(Trade.id == trade_id).first()
    if not trade: raise HTTPException(404, "Сделка не найдена")
    reporting = db.query(TradeReporting).filter_by(trade_id=trade_id).first()
    parties = db.query(Party).order_by(Party.display_name).all()
    profile = db.query(ReportingProfile).first()
    issues = validate_report(profile, reporting, trade) if profile and reporting else []
    return templates.TemplateResponse(request=request, name="trade_regulatory.html", context={"trade": trade, "reporting": reporting, "parties": parties, "profile": profile, "issues": issues, "currency_codes": CURRENCY_CODES, **reference_context()})


@app.post("/trades/{trade_id}/form1")
@app.post("/trades/{trade_id}/regulatory")
def save_trade_reporting(
    trade_id: int, client_party_id: int = Form(...), exchange_party_id: int = Form(...),
    message_number: int = Form(...), message_type: str = Form("1"), operation_date: str = Form(...),
    operation_code: str = Form("8001"), additional_operation_codes: str = Form("00"), currency_codes: str = Form("00"),
    client_participant_kind: str = Form("05"), exchange_participant_kind: str = Form("04"),
    kgs_equivalent: Decimal = Form(...), reason: str = Form(...), unusual_code: str = Form(...),
    unusual_codes: str = Form("00"), operation_state: str = Form("1"), extra_info: str = Form("00"),
    db: Session = Depends(get_db)
):
    trade = db.get(Trade, trade_id)
    if not trade: raise HTTPException(404, "Сделка не найдена")
    try: parsed_dt = datetime.fromisoformat(operation_date)
    except ValueError as exc: raise HTTPException(400, "Неверная дата операции") from exc
    reporting = db.query(TradeReporting).filter_by(trade_id=trade_id).first() or TradeReporting(trade_id=trade_id)
    reporting.client_party_id, reporting.exchange_party_id = client_party_id, exchange_party_id
    reporting.message_number, reporting.message_type, reporting.operation_date = message_number, message_type, parsed_dt
    reporting.operation_code, reporting.additional_operation_codes = operation_code.strip(), additional_operation_codes.strip() or "00"
    reporting.currency_codes, reporting.kgs_equivalent = (currency_codes.strip() or CURRENCY_CODES.get(trade.quote.rfq.quote_asset, "00")), kgs_equivalent
    reporting.client_participant_kind, reporting.exchange_participant_kind = client_participant_kind.strip(), exchange_participant_kind.strip()
    reporting.reason, reporting.unusual_code, reporting.unusual_codes = reason.strip(), unusual_code.strip(), unusual_codes.strip() or "00"
    reporting.operation_state, reporting.extra_info = operation_state, extra_info.strip() or "00"
    db.add(reporting); db.commit()
    return RedirectResponse(f"/trades/{trade_id}/form1", 303)


@app.get("/trades/{trade_id}/form1/validate")
def validate_trade_form1(trade_id: int, db: Session = Depends(get_db)):
    trade = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq)).filter(Trade.id == trade_id).first()
    reporting = db.query(TradeReporting).options(joinedload(TradeReporting.client_party), joinedload(TradeReporting.exchange_party)).filter_by(trade_id=trade_id).first()
    profile = db.query(ReportingProfile).first()
    if not trade:
        raise HTTPException(404, "Сделка не найдена")
    if not reporting or not profile:
        return {"valid": False, "issues": [{"field": "FORM1", "message": "Сначала заполните профиль подотчетного лица и Форму 1", "code": 5}]}
    issues = validate_report(profile, reporting, trade)
    return {"valid": not issues, "issues": [{"field": x.field, "message": x.message, "code": x.code} for x in issues]}


@app.get("/trades/{trade_id}/form1.xml")
@app.get("/trades/{trade_id}/regulatory.xml")
def export_trade_xml(trade_id: int, db: Session = Depends(get_db)):
    trade = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq)).filter(Trade.id == trade_id).first()
    reporting = db.query(TradeReporting).options(joinedload(TradeReporting.client_party), joinedload(TradeReporting.exchange_party)).filter_by(trade_id=trade_id).first()
    profile = db.query(ReportingProfile).first()
    if not trade or not reporting or not profile: raise HTTPException(400, "Сначала заполните профиль и регуляторные данные сделки")
    issues = validate_report(profile, reporting, trade)
    if issues: raise HTTPException(422, detail=[{"field": x.field, "message": x.message, "code": x.code} for x in issues])
    payload = generate_xml(profile, reporting, trade)
    return Response(content=payload, media_type="application/xml; charset=windows-1251", headers={"Content-Disposition": f"attachment; filename=form1_trade_{trade_id}.xml"})

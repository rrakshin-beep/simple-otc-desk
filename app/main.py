from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
import csv

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from .database import Base, engine, get_db
from .models import AmountType, Quote, RFQ, RFQHistory, RFQStatus, Trade, TradeHistory, TradeStatus


def reset_demo_database() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        Base.metadata.create_all(bind=engine)
        return

    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = OFF"))
        for table_name in ("trade_history", "rfq_history", "trades", "quotes", "rfqs"):
            if table_name in existing_tables:
                connection.execute(text(f"DELETE FROM {table_name}"))
        if "sqlite_sequence" in existing_tables:
            connection.execute(text("DELETE FROM sqlite_sequence WHERE name IN ('rfqs', 'quotes', 'trades', 'trade_history', 'rfq_history')"))
        connection.execute(text("PRAGMA foreign_keys = ON"))

    Base.metadata.create_all(bind=engine)


reset_demo_database()


def migrate_sqlite_schema() -> None:
    """Минимальная миграция старой демонстрационной SQLite-базы без Alembic."""
    inspector = inspect(engine)
    if "rfqs" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("rfqs")}
        with engine.begin() as connection:
            if "fiat_amount" not in columns:
                connection.execute(text("ALTER TABLE rfqs ADD COLUMN fiat_amount NUMERIC(30, 8)"))
            if "amount_type" not in columns:
                connection.execute(text("ALTER TABLE rfqs ADD COLUMN amount_type VARCHAR(16) DEFAULT 'CRYPTO'"))
                connection.execute(text("UPDATE rfqs SET amount_type = 'CRYPTO' WHERE amount_type IS NULL"))
    if "trades" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("trades")}
        with engine.begin() as connection:
            if "bank_fee" not in columns:
                connection.execute(text("ALTER TABLE trades ADD COLUMN bank_fee NUMERIC(30, 8) DEFAULT 0"))
            if "network_fee" not in columns:
                connection.execute(text("ALTER TABLE trades ADD COLUMN network_fee NUMERIC(30, 8) DEFAULT 0"))


migrate_sqlite_schema()

def migrate_legacy_trade_statuses() -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE trades SET status = 'APPROVED' "
                "WHERE status IN ('EXECUTED', 'SETTLED', 'COMPLETED')"
            )
        )

migrate_legacy_trade_statuses()

app = FastAPI(title="Simple OTC Desk")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

CRYPTO_ASSETS = {"USDT", "BTC", "USDC", "TRX", "ETH", "TON"}
FIAT_ASSETS = {"USD", "EUR", "KGS", "RUB"}

ALLOWED_TRANSITIONS = {
    TradeStatus.ACCEPTED: {TradeStatus.FUNDED, TradeStatus.CANCELLED},
    TradeStatus.FUNDED: {TradeStatus.AML_REVIEW, TradeStatus.APPROVED, TradeStatus.CANCELLED},
    TradeStatus.AML_REVIEW: {TradeStatus.APPROVED, TradeStatus.CANCELLED},
    TradeStatus.APPROVED: {TradeStatus.CANCELLED},
    TradeStatus.CANCELLED: set(),
}
REPORT_STATUS_COLUMNS = [
    TradeStatus.ACCEPTED,
    TradeStatus.FUNDED,
    TradeStatus.AML_REVIEW,
    TradeStatus.APPROVED,
    TradeStatus.CANCELLED,
]

def format_datetime(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M:%S") if value else "—"


def status_timestamps(trade: Trade) -> dict[str, str]:
    timestamps: dict[str, datetime] = {}
    for item in sorted(trade.history, key=lambda record: (record.created_at, record.id)):
        timestamps.setdefault(item.new_status, item.created_at)
    timestamps.setdefault(TradeStatus.ACCEPTED.value, trade.created_at)
    return {status.value: format_datetime(timestamps.get(status.value)) for status in REPORT_STATUS_COLUMNS}


templates.env.globals["format_datetime"] = format_datetime


def money(value: Decimal | float | None) -> Decimal:
    return Decimal(str(value or 0))


def calculate_amounts(rfq: RFQ, price: Decimal) -> tuple[Decimal, Decimal]:
    if price <= 0:
        raise HTTPException(400, "Цена должна быть больше нуля")
    if rfq.amount_type == AmountType.FIAT:
        fiat_amount = money(rfq.fiat_amount)
        if fiat_amount <= 0:
            raise HTTPException(400, "Сумма фиата должна быть больше нуля")
        crypto_amount = fiat_amount / price
    else:
        crypto_amount = money(rfq.amount)
        if crypto_amount <= 0:
            raise HTTPException(400, "Количество криптовалюты должно быть больше нуля")
        fiat_amount = crypto_amount * price
    return crypto_amount, fiat_amount


def trade_values(trade: Trade) -> dict[str, Decimal]:
    crypto_amount = money(trade.quote.rfq.amount)
    fiat_amount = money(trade.quote.rfq.fiat_amount)
    bank_fee = money(trade.bank_fee)
    network_fee = money(trade.network_fee)
    return {
        "crypto_amount": crypto_amount,
        "fiat_amount": fiat_amount,
        "bank_fee": bank_fee,
        "network_fee": network_fee,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    rfqs = db.query(RFQ).options(joinedload(RFQ.quote), selectinload(RFQ.rfq_history)).order_by(RFQ.id.desc()).all()
    trades = (
        db.query(Trade)
        .options(joinedload(Trade.quote).joinedload(Quote.rfq))
        .order_by(Trade.id.desc())
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"rfqs": rfqs, "trades": trades},
    )


@app.post("/rfqs")
def create_rfq(
    client_name: str = Form(...),
    side: str = Form(...),
    base_asset: str = Form(...),
    quote_asset: str = Form(...),
    amount_type: AmountType = Form(...),
    crypto_amount: str = Form(""),
    fiat_amount: str = Form(""),
    comment: str = Form(""),
    db: Session = Depends(get_db),
):
    base_asset = base_asset.upper().strip()
    quote_asset = quote_asset.upper().strip()
    if not base_asset or not quote_asset:
        raise HTTPException(400, "Укажите тикеры активов")
    try:
        crypto = Decimal(crypto_amount) if crypto_amount.strip() else None
        fiat = Decimal(fiat_amount) if fiat_amount.strip() else None
    except InvalidOperation as exc:
        raise HTTPException(400, "Количество должно быть числом") from exc

    if amount_type == AmountType.CRYPTO and (crypto is None or crypto <= 0):
        raise HTTPException(400, "Укажите количество криптовалюты")
    if amount_type == AmountType.FIAT and (fiat is None or fiat <= 0):
        raise HTTPException(400, "Укажите сумму фиата")

    rfq = RFQ(
        client_name=client_name.strip(),
        side=side,
        base_asset=base_asset,
        quote_asset=quote_asset,
        amount_type=amount_type,
        amount=crypto if amount_type == AmountType.CRYPTO else None,
        fiat_amount=fiat if amount_type == AmountType.FIAT else None,
        comment=comment.strip(),
    )
    db.add(rfq)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/rfqs/{rfq_id}/quote")
def create_quote(
    rfq_id: int,
    price: Decimal = Form(...),
    valid_minutes: int = Form(5),
    dealer_name: str = Form(...),
    quote_created_at: str = Form(""),
    db: Session = Depends(get_db),
):
    rfq = db.get(RFQ, rfq_id)
    if not rfq or rfq.status != RFQStatus.SUBMITTED:
        raise HTTPException(400, "RFQ недоступен для котирования")
    if price <= 0 or valid_minutes < 1:
        raise HTTPException(400, "Некорректные параметры котировки")

    crypto_amount, fiat_amount = calculate_amounts(rfq, price)
    rfq.amount = crypto_amount
    rfq.fiat_amount = fiat_amount
    try:
        created_at = datetime.fromisoformat(quote_created_at) if quote_created_at.strip() else datetime.utcnow()
    except ValueError as exc:
        raise HTTPException(400, "Некорректная дата и время создания котировки") from exc
    quote = Quote(
        rfq=rfq,
        price=price,
        expires_at=created_at + timedelta(minutes=valid_minutes),
        dealer_name=dealer_name.strip(),
        created_at=created_at,
    )
    rfq.status = RFQStatus.QUOTED
    db.add(quote)
    db.flush()
    db.add(
        RFQHistory(
            rfq_id=rfq.id,
            old_status=RFQStatus.SUBMITTED.value,
            new_status=RFQStatus.QUOTED.value,
            changed_by=dealer_name.strip(),
            note="Котировка создана",
            created_at=created_at,
        )
    )
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/quotes/{quote_id}/reject")
def reject_quote(
    quote_id: int,
    rejected_by: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    quote = (
        db.query(Quote)
        .options(joinedload(Quote.rfq), joinedload(Quote.trade))
        .filter(Quote.id == quote_id)
        .first()
    )
    if not quote:
        raise HTTPException(404, "Котировка не найдена")
    if quote.trade:
        raise HTTPException(400, "Принятую котировку отклонить нельзя")
    if quote.rfq.status != RFQStatus.QUOTED:
        raise HTTPException(400, "Котировка недоступна для отклонения")

    old_status = quote.rfq.status
    quote.rfq.status = RFQStatus.REJECTED
    db.add(
        RFQHistory(
            rfq_id=quote.rfq.id,
            old_status=old_status.value,
            new_status=RFQStatus.REJECTED.value,
            changed_by=rejected_by.strip() or "OTC Operator",
            note=reason.strip() or "Котировка отклонена",
        )
    )
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/quotes/{quote_id}/accept")
def accept_quote(quote_id: int, db: Session = Depends(get_db)):
    quote = (
        db.query(Quote)
        .options(joinedload(Quote.rfq), joinedload(Quote.trade))
        .filter(Quote.id == quote_id)
        .with_for_update()
        .first()
    )
    if not quote:
        raise HTTPException(404, "Котировка не найдена")
    if quote.trade or quote.rfq.status == RFQStatus.ACCEPTED:
        raise HTTPException(409, "Котировка уже принята")
    if quote.rfq.status != RFQStatus.QUOTED:
        raise HTTPException(400, "Котировка недоступна для принятия")
    if quote.expires_at < datetime.utcnow():
        raise HTTPException(400, "Срок котировки истек")

    quote.rfq.status = RFQStatus.ACCEPTED
    trade = Trade(quote=quote, status=TradeStatus.ACCEPTED, bank_fee=0, network_fee=0)
    db.add(trade)
    try:
        db.flush()
        db.add(
            TradeHistory(
                trade_id=trade.id,
                old_status=None,
                new_status=TradeStatus.ACCEPTED.value,
                changed_by=quote.rfq.client_name,
                note="Котировка принята клиентом",
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Котировка уже принята другой операцией") from exc
    return RedirectResponse(f"/trades/{trade.id}", status_code=303)


@app.post("/trades/{trade_id}/fees")
def update_fees(
    trade_id: int,
    bank_fee: Decimal = Form(0),
    network_fee: Decimal = Form(0),
    db: Session = Depends(get_db),
):
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Сделка не найдена")
    if bank_fee < 0 or network_fee < 0:
        raise HTTPException(400, "Комиссии не могут быть отрицательными")
    trade.bank_fee = bank_fee
    trade.network_fee = network_fee
    db.commit()
    return RedirectResponse(f"/trades/{trade_id}", status_code=303)


@app.post("/trades/{trade_id}/status")
def change_status(
    trade_id: int,
    new_status: TradeStatus = Form(...),
    changed_by: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Сделка не найдена")
    if new_status not in ALLOWED_TRANSITIONS[trade.status]:
        raise HTTPException(400, f"Переход {trade.status.value} → {new_status.value} запрещен")
    old = trade.status
    trade.status = new_status
    db.add(
        TradeHistory(
            trade_id=trade.id,
            old_status=old.value,
            new_status=new_status.value,
            changed_by=changed_by.strip(),
            note=note.strip(),
        )
    )
    db.commit()
    return RedirectResponse(f"/trades/{trade_id}", status_code=303)



@app.post("/trades/{trade_id}/edit")
def edit_trade(
    trade_id: int,
    client_name: str = Form(...),
    side: str = Form(...),
    base_asset: str = Form(...),
    quote_asset: str = Form(...),
    crypto_amount: Decimal = Form(...),
    price: Decimal = Form(...),
    bank_fee: Decimal = Form(0),
    network_fee: Decimal = Form(0),
    comment: str = Form(""),
    changed_by: str = Form("OTC Operator"),
    db: Session = Depends(get_db),
):
    trade = (
        db.query(Trade)
        .options(joinedload(Trade.quote).joinedload(Quote.rfq))
        .filter(Trade.id == trade_id)
        .first()
    )
    if not trade:
        raise HTTPException(404, "Сделка не найдена")

    base_asset = base_asset.upper().strip()
    quote_asset = quote_asset.upper().strip()
    if base_asset not in {"USDT", "BTC", "USDC", "TRX", "ETH", "TON"}:
        raise HTTPException(400, "Недопустимая криптовалюта")
    if quote_asset not in {"USD", "EUR", "KGS", "RUB"}:
        raise HTTPException(400, "Недопустимая фиатная валюта")
    if side not in {"BUY", "SELL"}:
        raise HTTPException(400, "Недопустимый тип операции")
    if crypto_amount <= 0 or price <= 0:
        raise HTTPException(400, "Количество и цена должны быть больше нуля")
    if bank_fee < 0 or network_fee < 0:
        raise HTTPException(400, "Комиссии не могут быть отрицательными")

    rfq = trade.quote.rfq
    old_values = (
        f"Клиент: {rfq.client_name}; операция: {rfq.side}; "
        f"пара: {rfq.base_asset}/{rfq.quote_asset}; "
        f"количество: {rfq.amount}; цена: {trade.quote.price}; "
        f"комиссия банка: {trade.bank_fee}; комиссия сети: {trade.network_fee}"
    )

    rfq.client_name = client_name.strip()
    rfq.side = side
    rfq.base_asset = base_asset
    rfq.quote_asset = quote_asset
    rfq.amount_type = AmountType.CRYPTO
    rfq.amount = crypto_amount
    rfq.fiat_amount = crypto_amount * price
    rfq.comment = comment.strip()
    trade.quote.price = price
    trade.bank_fee = bank_fee
    trade.network_fee = network_fee

    db.add(
        TradeHistory(
            trade_id=trade.id,
            old_status=trade.status.value,
            new_status=trade.status.value,
            changed_by=changed_by.strip() or "OTC Operator",
            note=f"Параметры сделки изменены. Было: {old_values}",
        )
    )
    db.commit()
    return RedirectResponse(f"/trades/{trade_id}", status_code=303)


@app.post("/trades/{trade_id}/delete")
def delete_trade(
    trade_id: int,
    db: Session = Depends(get_db),
):
    trade = (
        db.query(Trade)
        .options(joinedload(Trade.quote).joinedload(Quote.rfq))
        .filter(Trade.id == trade_id)
        .first()
    )
    if not trade:
        raise HTTPException(404, "Сделка не найдена")

    rfq = trade.quote.rfq
    rfq.status = RFQStatus.QUOTED
    db.delete(trade)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/trades/{trade_id}", response_class=HTMLResponse)
def trade_page(trade_id: int, request: Request, db: Session = Depends(get_db)):
    trade = (
        db.query(Trade)
        .options(joinedload(Trade.quote).joinedload(Quote.rfq).selectinload(RFQ.rfq_history), joinedload(Trade.history))
        .filter(Trade.id == trade_id)
        .first()
    )
    if not trade:
        raise HTTPException(404, "Сделка не найдена")
    allowed = sorted(ALLOWED_TRANSITIONS[trade.status], key=lambda x: x.value)
    return templates.TemplateResponse(
        request=request,
        name="trade.html",
        context={"trade": trade, "allowed": allowed, **trade_values(trade)},
    )


@app.get("/reports/trades", response_class=HTMLResponse)
def trades_report(request: Request, db: Session = Depends(get_db)):
    trades = (
        db.query(Trade)
        .options(joinedload(Trade.quote).joinedload(Quote.rfq), selectinload(Trade.history))
        .order_by(Trade.id.desc())
        .all()
    )
    rows = [{"trade": trade, "status_times": status_timestamps(trade), **trade_values(trade)} for trade in trades]
    return templates.TemplateResponse(
        request=request,
        name="report.html",
        context={"rows": rows, "status_columns": REPORT_STATUS_COLUMNS},
    )


@app.get("/reports/trades.csv")
def trades_report_csv(db: Session = Depends(get_db)):
    trades = (
        db.query(Trade)
        .options(joinedload(Trade.quote).joinedload(Quote.rfq), selectinload(Trade.history))
        .order_by(Trade.id.desc())
        .all()
    )
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["ID", "Дата создания", "Клиент", "Операция", "Пара", "Криптовалюта", "Цена", "Сумма сделки", "Фиатная валюта", "Комиссия банка", "Валюта комиссии банка", "Комиссия сети", "Валюта комиссии сети", "Текущий статус"] + [f"Дата и время: {status.value}" for status in REPORT_STATUS_COLUMNS])
    for trade in trades:
        values = trade_values(trade)
        rfq = trade.quote.rfq
        times = status_timestamps(trade)
        writer.writerow([trade.id, format_datetime(trade.created_at), rfq.client_name, rfq.side, f"{rfq.base_asset}/{rfq.quote_asset}", values["crypto_amount"], trade.quote.price, values["fiat_amount"], rfq.quote_asset, values["bank_fee"], rfq.quote_asset, values["network_fee"], rfq.base_asset, trade.status.value] + [times[status.value] for status in REPORT_STATUS_COLUMNS])
    data = "\ufeff" + buffer.getvalue()
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=otc_trades_report.csv"})


@app.get("/api/trades")
def api_trades(db: Session = Depends(get_db)):
    trades = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq)).all()
    result = []
    for trade in trades:
        values = trade_values(trade)
        rfq = trade.quote.rfq
        result.append({
            "id": trade.id,
            "status": trade.status.value,
            "client": rfq.client_name,
            "side": rfq.side,
            "pair": f"{rfq.base_asset}/{rfq.quote_asset}",
            "crypto_amount": float(values["crypto_amount"]),
            "price": float(trade.quote.price),
            "fiat_amount": float(values["fiat_amount"]),
            "bank_fee": float(values["bank_fee"]),
            "bank_fee_currency": rfq.quote_asset,
            "network_fee": float(values["network_fee"]),
            "network_fee_currency": rfq.base_asset,
        })
    return result

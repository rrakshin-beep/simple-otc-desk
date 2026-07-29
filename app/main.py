from datetime import datetime, timedelta
from decimal import Decimal
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from .database import Base, engine, get_db
from .models import Quote, RFQ, RFQStatus, Trade, TradeHistory, TradeStatus

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Simple OTC Desk")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

ALLOWED_TRANSITIONS = {
    TradeStatus.ACCEPTED: {TradeStatus.FUNDED, TradeStatus.CANCELLED},
    TradeStatus.FUNDED: {TradeStatus.AML_REVIEW, TradeStatus.APPROVED, TradeStatus.CANCELLED},
    TradeStatus.AML_REVIEW: {TradeStatus.APPROVED, TradeStatus.CANCELLED},
    TradeStatus.APPROVED: {TradeStatus.EXECUTED, TradeStatus.CANCELLED},
    TradeStatus.EXECUTED: {TradeStatus.SETTLED},
    TradeStatus.SETTLED: {TradeStatus.COMPLETED},
    TradeStatus.COMPLETED: set(),
    TradeStatus.CANCELLED: set(),
}

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    rfqs = db.query(RFQ).options(joinedload(RFQ.quote)).order_by(RFQ.id.desc()).all()
    trades = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq)).order_by(Trade.id.desc()).all()
    return templates.TemplateResponse("index.html", {"request": request, "rfqs": rfqs, "trades": trades})

@app.post("/rfqs")
def create_rfq(
    client_name: str = Form(...), side: str = Form(...), base_asset: str = Form(...),
    quote_asset: str = Form(...), amount: Decimal = Form(...), comment: str = Form(""),
    db: Session = Depends(get_db),
):
    if amount <= 0:
        raise HTTPException(400, "Количество должно быть больше нуля")
    rfq = RFQ(client_name=client_name.strip(), side=side, base_asset=base_asset.upper(),
              quote_asset=quote_asset.upper(), amount=amount, comment=comment.strip())
    db.add(rfq); db.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/rfqs/{rfq_id}/quote")
def create_quote(rfq_id: int, price: Decimal = Form(...), fee_rate: Decimal = Form(0),
                 valid_minutes: int = Form(5), dealer_name: str = Form(...), db: Session = Depends(get_db)):
    rfq = db.get(RFQ, rfq_id)
    if not rfq or rfq.status != RFQStatus.SUBMITTED:
        raise HTTPException(400, "RFQ недоступен для котирования")
    if price <= 0 or fee_rate < 0 or valid_minutes < 1:
        raise HTTPException(400, "Некорректные параметры котировки")
    quote = Quote(rfq=rfq, price=price, fee_rate=fee_rate,
                  expires_at=datetime.utcnow() + timedelta(minutes=valid_minutes), dealer_name=dealer_name.strip())
    rfq.status = RFQStatus.QUOTED
    db.add(quote); db.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/quotes/{quote_id}/accept")
def accept_quote(quote_id: int, db: Session = Depends(get_db)):
    quote = db.query(Quote).options(joinedload(Quote.rfq), joinedload(Quote.trade)).filter(Quote.id == quote_id).first()
    if not quote or quote.trade:
        raise HTTPException(400, "Котировка уже обработана")
    if quote.expires_at < datetime.utcnow():
        raise HTTPException(400, "Срок котировки истек")
    quote.rfq.status = RFQStatus.ACCEPTED
    trade = Trade(quote=quote, status=TradeStatus.ACCEPTED)
    db.add(trade); db.flush()
    db.add(TradeHistory(trade_id=trade.id, old_status=None, new_status=TradeStatus.ACCEPTED.value,
                        changed_by=quote.rfq.client_name, note="Котировка принята клиентом"))
    db.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/trades/{trade_id}/status")
def change_status(trade_id: int, new_status: TradeStatus = Form(...), changed_by: str = Form(...),
                  note: str = Form(""), db: Session = Depends(get_db)):
    trade = db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "Сделка не найдена")
    if new_status not in ALLOWED_TRANSITIONS[trade.status]:
        raise HTTPException(400, f"Переход {trade.status.value} → {new_status.value} запрещен")
    old = trade.status
    trade.status = new_status
    db.add(TradeHistory(trade_id=trade.id, old_status=old.value, new_status=new_status.value,
                        changed_by=changed_by.strip(), note=note.strip()))
    db.commit()
    return RedirectResponse(f"/trades/{trade_id}", status_code=303)

@app.get("/trades/{trade_id}", response_class=HTMLResponse)
def trade_page(trade_id: int, request: Request, db: Session = Depends(get_db)):
    trade = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq), joinedload(Trade.history)).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(404, "Сделка не найдена")
    allowed = sorted(ALLOWED_TRANSITIONS[trade.status], key=lambda x: x.value)
    gross = Decimal(str(trade.quote.price)) * Decimal(str(trade.quote.rfq.amount))
    fee = gross * Decimal(str(trade.quote.fee_rate)) / Decimal("100")
    return templates.TemplateResponse("trade.html", {"request": request, "trade": trade, "allowed": allowed,
        "gross": gross, "fee": fee, "total": gross + fee})

@app.get("/api/trades")
def api_trades(db: Session = Depends(get_db)):
    trades = db.query(Trade).options(joinedload(Trade.quote).joinedload(Quote.rfq)).all()
    return [{"id": t.id, "status": t.status.value, "client": t.quote.rfq.client_name,
             "pair": f"{t.quote.rfq.base_asset}/{t.quote.rfq.quote_asset}",
             "amount": float(t.quote.rfq.amount), "price": float(t.quote.price)} for t in trades]

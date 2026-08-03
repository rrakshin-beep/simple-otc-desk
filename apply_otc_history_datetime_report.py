from pathlib import Path


def read(path):
    return Path(path).read_text(encoding='utf-8')


def write(path, text):
    Path(path).write_text(text, encoding='utf-8')
    print(f'Обновлен: {path}')

# app/models.py
path = 'app/models.py'
text = read(path)

if 'rfq_history = relationship' not in text:
    text = text.replace(
        '    quote = relationship("Quote", back_populates="rfq", uselist=False, cascade="all, delete-orphan")',
        '    quote = relationship("Quote", back_populates="rfq", uselist=False, cascade="all, delete-orphan")\n'
        '    rfq_history = relationship("RFQHistory", back_populates="rfq", cascade="all, delete-orphan")',
        1,
    )

if 'class RFQHistory(Base):' not in text:
    marker = '\n\nclass Quote(Base):'
    block = '''

class RFQHistory(Base):
    __tablename__ = "rfq_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("rfqs.id"))
    old_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str] = mapped_column(String(32))
    changed_by: Mapped[str] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    rfq = relationship("RFQ", back_populates="rfq_history")
'''
    if marker not in text:
        raise SystemExit('Не найдена точка вставки RFQHistory в models.py')
    text = text.replace(marker, block + marker, 1)

write(path, text)

# app/main.py
path = 'app/main.py'
text = read(path)

text = text.replace(
    'from .models import AmountType, Quote, RFQ, RFQStatus, Trade, TradeHistory, TradeStatus',
    'from .models import AmountType, Quote, RFQ, RFQHistory, RFQStatus, Trade, TradeHistory, TradeStatus',
    1,
)

text = text.replace(
    'for table_name in ("trade_history", "trades", "quotes", "rfqs"):',
    'for table_name in ("trade_history", "rfq_history", "trades", "quotes", "rfqs"):',
    1,
)
text = text.replace(
    "('rfqs', 'quotes', 'trades', 'trade_history')",
    "('rfqs', 'quotes', 'trades', 'trade_history', 'rfq_history')",
    1,
)

text = text.replace(
    'rfqs = db.query(RFQ).options(joinedload(RFQ.quote)).order_by(RFQ.id.desc()).all()',
    'rfqs = db.query(RFQ).options(joinedload(RFQ.quote), selectinload(RFQ.rfq_history)).order_by(RFQ.id.desc()).all()',
    1,
)

old_signature = '''def create_quote(
    rfq_id: int,
    price: Decimal = Form(...),
    valid_minutes: int = Form(5),
    dealer_name: str = Form(...),
    db: Session = Depends(get_db),
):'''
new_signature = '''def create_quote(
    rfq_id: int,
    price: Decimal = Form(...),
    valid_minutes: int = Form(5),
    dealer_name: str = Form(...),
    quote_created_at: str = Form(""),
    db: Session = Depends(get_db),
):'''
if old_signature in text:
    text = text.replace(old_signature, new_signature, 1)

if 'created_at=created_at,' not in text:
    old = '''    crypto_amount, fiat_amount = calculate_amounts(rfq, price)
    rfq.amount = crypto_amount
    rfq.fiat_amount = fiat_amount
    quote = Quote(
        rfq=rfq,
        price=price,
        expires_at=datetime.utcnow() + timedelta(minutes=valid_minutes),
        dealer_name=dealer_name.strip(),
    )'''
    new = '''    crypto_amount, fiat_amount = calculate_amounts(rfq, price)
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
    )'''
    if old not in text:
        raise SystemExit('Не найден блок создания Quote в main.py')
    text = text.replace(old, new, 1)

if '@app.post("/quotes/{quote_id}/reject")' not in text:
    marker = '@app.post("/quotes/{quote_id}/accept")'
    endpoint = '''@app.post("/quotes/{quote_id}/reject")
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


'''
    if marker not in text:
        raise SystemExit('Не найдена точка вставки reject_quote')
    text = text.replace(marker, endpoint + marker, 1)

if 'note="Котировка создана"' not in text:
    old = '''    rfq.status = RFQStatus.QUOTED
    db.add(quote)
    db.commit()'''
    new = '''    rfq.status = RFQStatus.QUOTED
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
    db.commit()'''
    if old not in text:
        raise SystemExit('Не найден блок сохранения котировки')
    text = text.replace(old, new, 1)

text = text.replace(
    '    trade.quote.rfq.status = RFQStatus.QUOTED\n    db.delete(trade)',
    '''    rfq = trade.quote.rfq
    rfq.status = RFQStatus.QUOTED
    db.add(
        RFQHistory(
            rfq_id=rfq.id,
            old_status=RFQStatus.ACCEPTED.value,
            new_status=RFQStatus.QUOTED.value,
            changed_by="OTC Operator",
            note=f"Сделка #{trade.id} удалена независимо от ее статуса; котировка возвращена в QUOTED",
        )
    )
    db.delete(trade)''',
    1,
)

text = text.replace(
    '.options(joinedload(Trade.quote).joinedload(Quote.rfq), joinedload(Trade.history))',
    '.options(joinedload(Trade.quote).joinedload(Quote.rfq).selectinload(RFQ.rfq_history), joinedload(Trade.history))',
    1,
)

write(path, text)

# app/templates/index.html
path = 'app/templates/index.html'
text = read(path)

old = '<input name="dealer_name" value="OTC Dealer" required><button>Котировать</button>'
new = '<input name="dealer_name" value="OTC Dealer" required><label>Дата и время котировки<input name="quote_created_at" type="datetime-local"></label><button>Котировать</button>'
if old in text:
    text = text.replace(old, new, 1)

old = '<form method="post" action="/quotes/{{r.quote.id}}/accept"><button>Принять котировку</button></form>{% endif %}'
new = '''<form method="post" action="/quotes/{{r.quote.id}}/accept"><button>Принять котировку</button></form>
<form method="post" action="/quotes/{{r.quote.id}}/reject" class="inline" onsubmit="return confirm('Отклонить котировку? Операция останется в истории.');">
<input name="rejected_by" value="OTC Operator" required>
<input name="reason" placeholder="Причина отклонения">
<button type="submit">Отклонить котировку</button>
</form>{% endif %}'''
if old in text:
    text = text.replace(old, new, 1)

if '{% for h in r.rfq_history %}' not in text:
    old = '<span class="status">{{r.status.value}}</span>'
    new = '''<span class="status">{{r.status.value}}</span>
{% if r.rfq_history %}<details><summary>История заявки</summary>{% for h in r.rfq_history|sort(attribute='created_at') %}<div>{{format_datetime(h.created_at)}} — {{h.old_status or '—'}} → {{h.new_status}}; {{h.changed_by}}{% if h.note %}: {{h.note}}{% endif %}</div>{% endfor %}</details>{% endif %}'''
    text = text.replace(old, new, 1)

write(path, text)

# app/templates/trade.html
path = 'app/templates/trade.html'
text = read(path)

if '<h2>Данные заявки</h2>' not in text:
    marker = '<main>'
    section = '''<main>
<section class="card"><h2>Данные заявки</h2>
<p><b>Клиент:</b> {{trade.quote.rfq.client_name}}</p>
<p><b>Операция:</b> {{trade.quote.rfq.side}}</p>
<p><b>Пара:</b> {{trade.quote.rfq.base_asset}}/{{trade.quote.rfq.quote_asset}}</p>
<p><b>Вводная заявки:</b> {% if trade.quote.rfq.amount_type.value == 'FIAT' %}{{trade.quote.rfq.fiat_amount}} {{trade.quote.rfq.quote_asset}}{% else %}{{trade.quote.rfq.amount}} {{trade.quote.rfq.base_asset}}{% endif %}</p>
<p><b>Дата заявки:</b> {{format_datetime(trade.quote.rfq.created_at)}}</p>
<p><b>Дата котировки:</b> {{format_datetime(trade.quote.created_at)}}</p>
<p><b>Цена котировки:</b> {{trade.quote.price}} {{trade.quote.rfq.quote_asset}}</p>
</section>'''
    if marker not in text:
        raise SystemExit('Не найден main в trade.html')
    text = text.replace(marker, section, 1)

text = text.replace(
    '<h2>Удаление сделки</h2>',
    '<h2>Удаление сделки</h2><p>Доступно на любом этапе, включая завершенную сделку.</p>',
    1,
)

write(path, text)

# app/templates/report.html
path = 'app/templates/report.html'
report = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Отчет по сделкам</title><link rel="stylesheet" href="/static/style.css"></head><body>
<header><div><h1>Отчет по сделкам</h1><a href="/">← На главную</a></div><a class="header-link" href="/reports/trades.csv">Скачать CSV</a></header><main>
<section class="card"><p>Каждая строка соответствует одной сделке. Суммирование показателей не выполняется.</p><div class="table"><table>
<tr><th>ID</th><th>Дата заявки</th><th>Дата котировки</th><th>Дата сделки</th><th>Клиент</th><th>Операция</th><th>Пара</th><th>Тип вводной</th><th>Данные заявки</th><th>Цена</th><th>Комиссия банка</th><th>Комиссия сети</th><th>Статус</th>{% for status in status_columns %}<th>{{status.value}}</th>{% endfor %}</tr>
{% for row in rows %}{% set t=row.trade %}{% set r=t.quote.rfq %}
<tr><td><a href="/trades/{{t.id}}">#{{t.id}}</a></td><td>{{format_datetime(r.created_at)}}</td><td>{{format_datetime(t.quote.created_at)}}</td><td>{{format_datetime(t.created_at)}}</td><td>{{r.client_name}}</td><td>{{r.side}}</td><td>{{r.base_asset}}/{{r.quote_asset}}</td><td>{{r.amount_type.value}}</td><td>{% if r.amount_type.value == 'FIAT' %}{{r.fiat_amount}} {{r.quote_asset}}{% else %}{{r.amount}} {{r.base_asset}}{% endif %}</td><td>{{t.quote.price}} {{r.quote_asset}}</td><td>{{row.bank_fee}} {{r.quote_asset}}</td><td>{{row.network_fee}} {{r.base_asset}}</td><td>{{t.status.value}}</td>{% for status in status_columns %}<td>{{row.status_times[status.value]}}</td>{% endfor %}</tr>
{% endfor %}
</table></div></section></main></body></html>'''
write(path, report)

print('\nИзменения внесены. Выполните:')
print('python3 -m compileall app')
print('PYTHONPATH=. pytest -q')

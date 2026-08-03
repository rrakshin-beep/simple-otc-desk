
from pathlib import Path

def read(path):
    return Path(path).read_text(encoding="utf-8")

def write(path, text):
    Path(path).write_text(text, encoding="utf-8")
    print(f"Обновлен: {path}")

# ---------- app/main.py ----------
path = "app/main.py"
text = read(path)

edit_endpoint = r'''
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


'''

# Remove older delete endpoint if present, then insert fresh edit/delete endpoints.
if '@app.post("/trades/{trade_id}/delete")' in text:
    start = text.index('@app.post("/trades/{trade_id}/delete")')
    marker = '@app.get("/trades/{trade_id}", response_class=HTMLResponse)'
    end = text.index(marker, start)
    text = text[:start] + text[end:]

marker = '@app.get("/trades/{trade_id}", response_class=HTMLResponse)'
if '@app.post("/trades/{trade_id}/edit")' not in text:
    if marker not in text:
        raise SystemExit("Не найдена точка вставки перед trade_page")
    text = text.replace(marker, edit_endpoint + marker, 1)

write(path, text)

# ---------- app/templates/trade.html ----------
path = "app/templates/trade.html"
text = read(path)

edit_section = r'''
<section class="card">
<h2>Редактирование сделки</h2>
<form method="post" action="/trades/{{trade.id}}/edit" class="grid" id="trade-edit-form">
<label>Клиент<input name="client_name" value="{{trade.quote.rfq.client_name}}" required></label>
<label>Операция<select name="side"><option value="BUY" {% if trade.quote.rfq.side == 'BUY' %}selected{% endif %}>Купить</option><option value="SELL" {% if trade.quote.rfq.side == 'SELL' %}selected{% endif %}>Продать</option></select></label>
<label>Криптовалюта<select name="base_asset">
<option value="USDT" {% if trade.quote.rfq.base_asset == 'USDT' %}selected{% endif %}>USDT</option>
<option value="BTC" {% if trade.quote.rfq.base_asset == 'BTC' %}selected{% endif %}>Bitcoin (BTC)</option>
<option value="USDC" {% if trade.quote.rfq.base_asset == 'USDC' %}selected{% endif %}>USDC</option>
<option value="TRX" {% if trade.quote.rfq.base_asset == 'TRX' %}selected{% endif %}>Tron (TRX)</option>
<option value="ETH" {% if trade.quote.rfq.base_asset == 'ETH' %}selected{% endif %}>Эфир (ETH)</option>
<option value="TON" {% if trade.quote.rfq.base_asset == 'TON' %}selected{% endif %}>TON</option>
</select></label>
<label>Фиатная валюта<select name="quote_asset">
<option value="USD" {% if trade.quote.rfq.quote_asset == 'USD' %}selected{% endif %}>USD</option>
<option value="EUR" {% if trade.quote.rfq.quote_asset == 'EUR' %}selected{% endif %}>EUR</option>
<option value="KGS" {% if trade.quote.rfq.quote_asset == 'KGS' %}selected{% endif %}>KGS</option>
<option value="RUB" {% if trade.quote.rfq.quote_asset == 'RUB' %}selected{% endif %}>RUB</option>
</select></label>
<label>Количество криптовалюты<input name="crypto_amount" id="edit-crypto-amount" type="number" step="0.00000001" min="0.00000001" value="{{crypto_amount}}" required></label>
<label>Цена<input name="price" id="edit-price" type="number" step="0.00000001" min="0.00000001" value="{{trade.quote.price}}" required></label>
<label>Расчетная сумма сделки<input id="edit-fiat-amount" type="number" step="0.00000001" value="{{fiat_amount}}" readonly></label>
<label>Комиссия банка<input name="bank_fee" type="number" step="0.00000001" min="0" value="{{bank_fee}}"></label>
<label>Комиссия сети<input name="network_fee" type="number" step="0.00000001" min="0" value="{{network_fee}}"></label>
<label>Комментарий<input name="comment" value="{{trade.quote.rfq.comment or ''}}"></label>
<label>Кто изменил<input name="changed_by" value="OTC Operator" required></label>
<button type="submit">Сохранить изменения</button>
</form>
</section>
<section class="card">
<h2>Удаление сделки</h2>
<p>Удаление доступно при любом статусе. Связанная котировка вернется в статус QUOTED.</p>
<form method="post" action="/trades/{{trade.id}}/delete" onsubmit="return confirm('Удалить сделку #{{trade.id}}? Действие нельзя отменить.');">
<button type="submit">Удалить сделку</button>
</form>
</section>
'''

# Remove previous edit/delete sections if this script is rerun.
if '<h2>Редактирование сделки</h2>' in text:
    start = text.index('<section class="card">\n<h2>Редактирование сделки</h2>')
    end_marker = '<section class="card"><h2>История</h2>'
    end = text.index(end_marker, start)
    text = text[:start] + text[end:]

history_marker = '<section class="card"><h2>История</h2>'
if history_marker not in text:
    raise SystemExit("Не найдена секция История в trade.html")
text = text.replace(history_marker, edit_section + history_marker, 1)

# Add recalculation script before closing body.
calc_script = r'''
<script>
const editCrypto = document.getElementById('edit-crypto-amount');
const editPrice = document.getElementById('edit-price');
const editFiat = document.getElementById('edit-fiat-amount');
function recalculateTrade() {
  const crypto = Number(editCrypto.value || 0);
  const price = Number(editPrice.value || 0);
  editFiat.value = (crypto * price).toFixed(8);
}
editCrypto.addEventListener('input', recalculateTrade);
editPrice.addEventListener('input', recalculateTrade);
recalculateTrade();
</script>
'''
if "function recalculateTrade()" not in text:
    text = text.replace("</main></body></html>", "</main>" + calc_script + "</body></html>", 1)

write(path, text)

print("\nГотово. Выполните:")
print("python3 -m compileall app")
print("PYTHONPATH=. pytest -q")


from pathlib import Path
import re

def read(path):
    return Path(path).read_text(encoding="utf-8")

def write(path, text):
    Path(path).write_text(text, encoding="utf-8")
    print(f"Обновлен: {path}")

path = "app/models.py"
text = read(path)
text = re.sub(r'^\s+EXECUTED = "EXECUTED"\n', '', text, flags=re.M)
text = re.sub(r'^\s+SETTLED = "SETTLED"\n', '', text, flags=re.M)
text = re.sub(r'^\s+COMPLETED = "COMPLETED"\n', '', text, flags=re.M)
write(path, text)

path = "app/main.py"
text = read(path)

if "CRYPTO_ASSETS =" not in text:
    text = text.replace(
        "ALLOWED_TRANSITIONS = {",
        'CRYPTO_ASSETS = {"USDT", "BTC", "USDC", "TRX", "ETH", "TON"}\n'
        'FIAT_ASSETS = {"USD", "EUR", "KGS", "RUB"}\n\n'
        "ALLOWED_TRANSITIONS = {",
        1,
    )

start = text.index("ALLOWED_TRANSITIONS = {")
end_marker = "]\n\n\ndef format_datetime"
end = text.index(end_marker, start) + 2
new_block = (
    "ALLOWED_TRANSITIONS = {\n"
    "    TradeStatus.ACCEPTED: {TradeStatus.FUNDED, TradeStatus.CANCELLED},\n"
    "    TradeStatus.FUNDED: {TradeStatus.AML_REVIEW, TradeStatus.APPROVED, TradeStatus.CANCELLED},\n"
    "    TradeStatus.AML_REVIEW: {TradeStatus.APPROVED, TradeStatus.CANCELLED},\n"
    "    TradeStatus.APPROVED: {TradeStatus.CANCELLED},\n"
    "    TradeStatus.CANCELLED: set(),\n"
    "}\n"
    "REPORT_STATUS_COLUMNS = [\n"
    "    TradeStatus.ACCEPTED,\n"
    "    TradeStatus.FUNDED,\n"
    "    TradeStatus.AML_REVIEW,\n"
    "    TradeStatus.APPROVED,\n"
    "    TradeStatus.CANCELLED,\n"
    "]"
)
text = text[:start] + new_block + text[end:]

if "def migrate_legacy_trade_statuses" not in text:
    migration = (
        "migrate_sqlite_schema()\n\n"
        "def migrate_legacy_trade_statuses() -> None:\n"
        "    with engine.begin() as connection:\n"
        "        connection.execute(\n"
        "            text(\n"
        "                \"UPDATE trades SET status = 'APPROVED' \"\n"
        "                \"WHERE status IN ('EXECUTED', 'SETTLED', 'COMPLETED')\"\n"
        "            )\n"
        "        )\n\n"
        "migrate_legacy_trade_statuses()\n\n"
        "app = FastAPI"
    )
    text = text.replace("migrate_sqlite_schema()\n\napp = FastAPI", migration, 1)

needle = (
    "    try:\n"
    "        crypto = Decimal(crypto_amount) if crypto_amount.strip() else None\n"
    "        fiat = Decimal(fiat_amount) if fiat_amount.strip() else None"
)
if 'raise HTTPException(400, "Недопустимая криптовалюта")' not in text:
    replacement = (
        "    base_asset = base_asset.upper().strip()\n"
        "    quote_asset = quote_asset.upper().strip()\n"
        "    if base_asset not in CRYPTO_ASSETS:\n"
        "        raise HTTPException(400, \"Недопустимая криптовалюта\")\n"
        "    if quote_asset not in FIAT_ASSETS:\n"
        "        raise HTTPException(400, \"Недопустимая фиатная валюта\")\n"
        "    try:\n"
        "        crypto = Decimal(crypto_amount) if crypto_amount.strip() else None\n"
        "        fiat = Decimal(fiat_amount) if fiat_amount.strip() else None"
    )
    if needle not in text:
        raise SystemExit("Не найден блок create_rfq в app/main.py")
    text = text.replace(needle, replacement, 1)

text = text.replace(
    "        base_asset=base_asset.upper().strip(),\n"
    "        quote_asset=quote_asset.upper().strip(),",
    "        base_asset=base_asset,\n"
    "        quote_asset=quote_asset,",
    1,
)

if '@app.post("/trades/{trade_id}/delete")' not in text:
    marker = '@app.get("/trades/{trade_id}", response_class=HTMLResponse)'
    endpoint = (
        '@app.post("/trades/{trade_id}/delete")\n'
        "def delete_trade(trade_id: int, db: Session = Depends(get_db)):\n"
        "    trade = (\n"
        "        db.query(Trade)\n"
        "        .options(joinedload(Trade.quote).joinedload(Quote.rfq))\n"
        "        .filter(Trade.id == trade_id)\n"
        "        .first()\n"
        "    )\n"
        "    if not trade:\n"
        "        raise HTTPException(404, \"Сделка не найдена\")\n"
        "    trade.quote.rfq.status = RFQStatus.QUOTED\n"
        "    db.delete(trade)\n"
        "    db.commit()\n"
        "    return RedirectResponse(\"/\", status_code=303)\n\n\n"
    )
    if marker not in text:
        raise SystemExit("Не найдена точка вставки delete_trade в app/main.py")
    text = text.replace(marker, endpoint + marker, 1)

write(path, text)

path = "app/templates/index.html"
text = read(path)
old = '<input name="base_asset" value="BTC" placeholder="Криптовалюта (BTC)" required><input name="quote_asset" placeholder="Фиатная валюта (USD, EUR, KGS, RUB)" required>'
new = '<select name="base_asset" required><option value="USDT">USDT</option><option value="BTC">Bitcoin (BTC)</option><option value="USDC">USDC</option><option value="TRX">Tron (TRX)</option><option value="ETH">Эфир (ETH)</option><option value="TON">TON</option></select><select name="quote_asset" required><option value="USD">USD</option><option value="EUR">EUR</option><option value="KGS">KGS</option><option value="RUB">RUB</option></select>'
if old in text:
    text = text.replace(old, new, 1)
elif '<select name="base_asset"' not in text:
    raise SystemExit("Не найдены поля валют в app/templates/index.html")
write(path, text)

path = "app/templates/trade.html"
text = read(path)
if '/delete"' not in text:
    marker = '<section class="card"><h2>История</h2>'
    block = '<section class="card"><h2>Удаление сделки</h2><form method="post" action="/trades/{{trade.id}}/delete" onsubmit="return confirm(\'Удалить сделку #{{trade.id}}? Это действие нельзя отменить.\');"><button type="submit">Удалить сделку</button></form></section>\n'
    if marker not in text:
        raise SystemExit("Не найдена секция истории в app/templates/trade.html")
    text = text.replace(marker, block + marker, 1)
write(path, text)

print("\nГотово. Проверьте: python3 -m compileall app && pytest -q")

import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parents[1])

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_crypto_based_quote_and_acceptance():
    response = client.post("/rfqs", data={
        "client_name": "Test Crypto",
        "side": "BUY",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "amount_type": "CRYPTO",
        "crypto_amount": "2",
        "fiat_amount": "",
        "comment": "",
    }, follow_redirects=False)
    assert response.status_code == 303
    home = client.get("/")
    assert home.status_code == 200

    response = client.post("/rfqs/1/quote", data={"price": "50000", "valid_minutes": "5", "dealer_name": "Dealer"}, follow_redirects=False)
    assert response.status_code == 303

    response = client.post("/quotes/1/accept", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/trades/1"

    duplicate = client.post("/quotes/1/accept", follow_redirects=False)
    assert duplicate.status_code == 409

    api = client.get("/api/trades").json()
    assert api[0]["crypto_amount"] == 2.0
    assert api[0]["fiat_amount"] == 100000.0


def test_fiat_based_calculation_and_report():
    client.post("/rfqs", data={
        "client_name": "Test Fiat",
        "side": "SELL",
        "base_asset": "ETH",
        "quote_asset": "USD",
        "amount_type": "FIAT",
        "crypto_amount": "",
        "fiat_amount": "6000",
        "comment": "",
    })
    response = client.post("/rfqs/2/quote", data={"price": "3000", "valid_minutes": "5", "dealer_name": "Dealer"}, follow_redirects=False)
    assert response.status_code == 303
    client.post("/quotes/2/accept")
    client.post("/trades/2/fees", data={"bank_fee": "25", "network_fee": "5"})

    api = client.get("/api/trades").json()
    trade = next(item for item in api if item["id"] == 2)
    assert trade["crypto_amount"] == 2.0
    assert trade["fiat_amount"] == 6000.0
    assert trade["bank_fee"] == 25.0
    assert trade["network_fee"] == 5.0
    assert trade["bank_fee_currency"] == "USD"
    assert trade["network_fee_currency"] == "ETH"

    report = client.get("/reports/trades")
    assert report.status_code == 200
    assert "Комиссия банка" in report.text
    assert "Комиссия сети" in report.text
    assert "ACCEPTED — дата и время" in report.text
    assert "FUNDED — дата и время" in report.text

    csv_report = client.get("/reports/trades.csv")
    assert csv_report.status_code == 200
    assert "otc_trades_report.csv" in csv_report.headers["content-disposition"]
    assert "Дата и время: ACCEPTED" in csv_report.text

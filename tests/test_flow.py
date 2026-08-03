import os
from pathlib import Path
from uuid import uuid4

os.chdir(Path(__file__).resolve().parents[1])

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_full_flow_and_idempotency():
    r = client.post('/rfqs', data={
        'client_name':'Client A','side':'BUY','base_asset':'BTC','quote_asset':'RUB','network':'Bitcoin',
        'amount_type':'CRYPTO','crypto_amount':'2','fiat_amount':'','comment':'test'
    }, follow_redirects=False)
    assert r.status_code == 303
    r = client.post('/rfqs/1/quote', data={'price':'5000000','valid_minutes':'5','dealer_name':'Dealer'}, follow_redirects=False)
    assert r.status_code == 303
    key = str(uuid4())
    r = client.post('/quotes/1/accept', data={'idempotency_key':key,'confirm':'yes'}, follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'] == '/trades/1'
    duplicate = client.post('/quotes/1/accept', data={'idempotency_key':key,'confirm':'yes'}, follow_redirects=False)
    assert duplicate.status_code == 303
    details = client.post('/trades/1/details', data={
        'bank_fee':'1000','network_fee':'0.0001','bank_fee_payer':'CLIENT','network_fee_payer':'EXCHANGE',
        'bank_reference':'BANK-1','tx_hash':'abc','aml_risk':'LOW','actor':'Operator'
    }, follow_redirects=False)
    assert details.status_code == 303
    api = client.get('/api/trades').json()[0]
    assert api['fiat_amount'] == 10000000.0
    assert api['bank_fee_currency'] == 'RUB'
    assert api['network_fee_currency'] == 'BTC'


def test_reject_report_and_archive():
    client.post('/rfqs', data={
        'client_name':'Client B','side':'SELL','base_asset':'USDT','quote_asset':'USD','network':'TRC-20',
        'amount_type':'FIAT','crypto_amount':'','fiat_amount':'6000','comment':''
    })
    client.post('/rfqs/2/quote', data={'price':'1','valid_minutes':'5','dealer_name':'Dealer'})
    reject = client.post('/quotes/2/reject', data={'rejected_by':'Client B','reason':'Не устраивает цена'}, follow_redirects=False)
    assert reject.status_code == 303
    report = client.get('/reports/trades')
    assert report.status_code == 200
    assert 'Комиссия банка' in report.text and 'Комиссия сети' in report.text
    assert client.get('/reports/trades.csv').status_code == 200
    assert client.get('/reports/trades.xlsx').status_code == 200
    archived = client.post('/trades/1/archive', data={'archived_by':'Admin','reason':'Тест архива'}, follow_redirects=False)
    assert archived.status_code == 303
    assert client.get('/api/trades').json() == []

def test_regulatory_profile_parties_validation_and_xml():
    profile = client.post('/settings/reporting', data={
        'inn':'0150520251068','person_name':'Общество с ограниченной ответственностью Корэкс Маркетс',
        'org_kind':'28','bank_bic':'00','okpo':'33901014','orgform_code':'20','branch':'00',
        'legal_postcode':'720001','legal_town_code':'41711000000000','legal_region':'БИШКЕК','legal_area':'00',
        'legal_town':'Г. БИШКЕК','legal_street':'Исанова','legal_house':'102','legal_room':'1',
        'performer_name':'Комплаенс Офицер','performer_post':'комплаенс-офицер','phone':'996000000000'
    }, follow_redirects=False)
    assert profile.status_code == 303
    exchange = client.post('/parties', data={
        'party_type':'LEGAL','display_name':'Корэкс Маркетс','inn':'0150520251068','okpo':'33901014',
        'country_code':'417','resident_code':'1','orgform_code':'20','registration_number':'316103-3301-ООО',
        'registration_authority':'Управление юстиции','activity':'Управление финансовыми рынками',
        'last_name':'00','first_name':'00','middle_name':'00','document_code':'00','document_series':'00',
        'document_number':'00','birth_place':'00','legal_postcode':'720001','legal_town_code':'41711000000000',
        'legal_region':'БИШКЕК','legal_area':'00','legal_town':'Г. БИШКЕК','legal_street':'Исанова',
        'legal_house':'102','legal_room':'1','account_number':'TExchangeWallet','account_bank':'00',
        'account_bic':'00','account_country_code':'00','account_address':'00'
    }, follow_redirects=False)
    assert exchange.status_code == 303
    physical = client.post('/parties', data={
        'party_type':'PHYSICAL','display_name':'Иванов Иван','inn':'12345678901234','okpo':'00','country_code':'643',
        'resident_code':'2','orgform_code':'00','registration_number':'00','registration_authority':'00','activity':'00',
        'last_name':'Иванов','first_name':'Иван','middle_name':'Иванович','document_code':'01','document_series':'00',
        'document_number':'1234567890','birth_place':'Россия','legal_postcode':'00','legal_town_code':'643',
        'legal_region':'Москва','legal_area':'00','legal_town':'Москва','legal_street':'Тверская',
        'legal_house':'1','legal_room':'1','account_number':'TClientWallet','account_bank':'00',
        'account_bic':'00','account_country_code':'00','account_address':'00'
    }, follow_redirects=False)
    assert physical.status_code == 303
    report = client.post('/trades/1/regulatory', data={
        'client_party_id':'2','exchange_party_id':'1','message_number':'1','message_type':'1',
        'operation_date':'2026-07-29T21:59','operation_code':'8001','additional_operation_codes':'2004',
        'currency_codes':'643','kgs_equivalent':'10000000','reason':'согласно ордеру клиента',
        'unusual_code':'1000','unusual_codes':'00','operation_state':'1',
        'extra_info':'Покупка BTC за рубли'
    }, follow_redirects=False)
    assert report.status_code == 303
    page = client.get('/trades/1/regulatory')
    assert page.status_code == 200 and 'Проверка пройдена' in page.text
    xml = client.get('/trades/1/regulatory.xml')
    assert xml.status_code == 200
    decoded = xml.content.decode('windows-1251')
    assert '<ROWSET>' in decoded and '<MSGNUM>1</MSGNUM>' in decoded and '<PAGENUM>003Ф</PAGENUM>' in decoded

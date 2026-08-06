from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from xml.etree.ElementTree import Element, SubElement, tostring

from .reference_data import code_exists, currency_numeric_by_alpha

CURRENCY_CODES = {asset: currency_numeric_by_alpha(asset) or "00" for asset in ("KGS", "RUB", "USD", "EUR")}

@dataclass
class ValidationIssue:
    field: str
    message: str
    code: int = 5


def value_or_00(value) -> str:
    if value is None:
        return "00"
    text = str(value).strip()
    return text if text else "00"


def validate_report(profile, reporting, trade) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    def required(field, value, message=None, zero_missing=True):
        empty_values = {""} | ({"00"} if zero_missing else set())
        if value is None or str(value).strip() in empty_values:
            issues.append(ValidationIssue(field, message or "Обязательное поле не заполнено"))
    required("PERSON.REGNUM", getattr(profile, "inn", None), "Не указан ИНН подотчетного лица")
    required("PERSON.PERSON_NAME", getattr(profile, "person_name", None), "Не указано наименование подотчетного лица")
    required("OPER.OPERDATE", reporting.operation_date, "Не указаны дата и время операции")
    required("OPER.OPER_CODE", reporting.operation_code, "Не указан вид операции")
    required("OPER.SUM", reporting.kgs_equivalent, "Не указан сомовый эквивалент")
    required("OPER.UNUSUAL_CODE", reporting.unusual_code, "Не указан критерий/признак контроля")
    if not reporting.client_party:
        issues.append(ValidationIssue("PRTLIST.CLIENT", "Не выбран клиент — участник операции"))
    if not reporting.exchange_party:
        issues.append(ValidationIssue("PRTLIST.EXCHANGE", "Не выбран участник со стороны биржи"))
    for role, party in (("CLIENT", reporting.client_party), ("EXCHANGE", reporting.exchange_party)):
        if not party:
            continue
        required(f"{role}.REGNUM", party.inn, "Не указан ИНН участника")
        required(f"{role}.ACCOUNT.NUM", party.account_number, "Не указан счет или адрес кошелька")
        required(f"{role}.ACCOUNT.BIC", party.account_bic, "Не указан БИК (при отсутствии укажите 00)", zero_missing=False)
        required(f"{role}.ACCOUNT.COUNTRY_CODE", party.account_country_code, "Не указан код страны банка (при отсутствии укажите 00)", zero_missing=False)
        if party.party_type.value == "PHYSICAL":
            required(f"{role}.NATION_CODE", party.country_code, "Не указано гражданство")
            required(f"{role}.PHYSNAME.LASTNAME", party.last_name, "Не указана фамилия")
            required(f"{role}.PHYSNAME.FIRSTNAME", party.first_name, "Не указано имя")
        else:
            required(f"{role}.JURNAME.ORGNAME", party.display_name, "Не указано наименование юридического лица")
            required(f"{role}.ADDINFO.ACTIVITY", party.activity, "Не указан основной вид деятельности", zero_missing=False)
            required(f"{role}.ADDINFO.ACTIVITIES", party.additional_activities, "Не указаны дополнительные виды деятельности (при отсутствии укажите 00)", zero_missing=False)
            required(f"{role}.DISPONENT.NAME", party.authorized_person_name, "Не указано уполномоченное лицо (при отсутствии укажите 00)", zero_missing=False)
    if reporting.client_participant_kind and not code_exists("participant_kinds", reporting.client_participant_kind):
        issues.append(ValidationIssue("CLIENT.PARTIC_KIND", f"Тип участника {reporting.client_participant_kind} отсутствует в справочнике", 4))
    if reporting.exchange_participant_kind and not code_exists("participant_kinds", reporting.exchange_participant_kind):
        issues.append(ValidationIssue("EXCHANGE.PARTIC_KIND", f"Тип участника {reporting.exchange_participant_kind} отсутствует в справочнике", 4))
    rfq = trade.quote.rfq
    if rfq.quote_asset not in CURRENCY_CODES:
        issues.append(ValidationIssue("OPER.CUR_CODES", f"Нет цифрового кода для валюты {rfq.quote_asset}", 4))
    if Decimal(str(rfq.fiat_amount or 0)) <= 0:
        issues.append(ValidationIssue("OPER.SUMCUR", "Сумма в исходной валюте должна быть больше нуля", 6))
    if Decimal(str(rfq.amount or 0)) <= 0:
        issues.append(ValidationIssue("OPER.SUMTGT", "Сумма в целевой валюте должна быть больше нуля", 6))
    if reporting.operation_code and not code_exists("operation_codes", reporting.operation_code):
        issues.append(ValidationIssue("OPER.OPER_CODE", f"Код операции {reporting.operation_code} отсутствует в справочнике", 4))
    for code in [c.strip() for c in str(reporting.additional_operation_codes or "").split(",") if c.strip() and c.strip() != "00"]:
        if not code_exists("operation_codes", code):
            issues.append(ValidationIssue("OPER.OPER_CODES", f"Дополнительный код операции {code} отсутствует в справочнике", 4))
    if reporting.unusual_code and reporting.unusual_code != "00" and not code_exists("unusual_codes", reporting.unusual_code):
        issues.append(ValidationIssue("OPER.UNUSUAL_CODE", f"Код {reporting.unusual_code} отсутствует в справочнике критериев/признаков", 4))
    for code in [c.strip() for c in str(reporting.unusual_codes or "").split(",") if c.strip() and c.strip() != "00"]:
        if not code_exists("unusual_codes", code):
            issues.append(ValidationIssue("OPER.UNUSUAL_CODES", f"Код {code} отсутствует в справочнике критериев/признаков", 4))
    for role, party in (("CLIENT", reporting.client_party), ("EXCHANGE", reporting.exchange_party)):
        if not party:
            continue
        if party.party_type.value == "LEGAL" and not code_exists("organization_forms", party.orgform_code):
            issues.append(ValidationIssue(f"{role}.JURNAME.ORGFORM_CODE", f"Код ОПФ {party.orgform_code} отсутствует в справочнике", 4))
        if party.party_type.value == "PHYSICAL" and party.document_code != "00" and not code_exists("document_codes", party.document_code):
            issues.append(ValidationIssue(f"{role}.DOC.DOC_CODE", f"Код документа {party.document_code} отсутствует в справочнике", 4))
        if party.party_type.value == "LEGAL" and party.authorized_document_code != "00" and not code_exists("document_codes", party.authorized_document_code):
            issues.append(ValidationIssue(f"{role}.DISPONENT.DOC.DOC_CODE", f"Код документа уполномоченного лица {party.authorized_document_code} отсутствует в справочнике", 4))
        if party.country_code != "00" and not code_exists("countries", party.country_code.lstrip("0") or "0") and not code_exists("countries", party.country_code):
            issues.append(ValidationIssue(f"{role}.NATION_CODE", f"Код страны {party.country_code} отсутствует в справочнике", 4))
    return issues


def add_text(parent, tag, value):
    SubElement(parent, tag).text = value_or_00(value)


def add_address(parent, tag, p, prefix):
    a = SubElement(parent, tag)
    for xml_tag, suffix in [("POSTCODE","postcode"),("TOWN_CODE","town_code"),("REGION","region"),("AREA","area"),("TOWN","town"),("STREET","street"),("HOUSE","house")]:
        add_text(a, xml_tag, getattr(p, f"{prefix}_{suffix}", "00"))
    add_text(a, "CORP", "00"); add_text(a, "BUILDING", "00")
    add_text(a, "ROOM", getattr(p, f"{prefix}_room", "00"))


def add_party(prtlist, num, page, party, participant_kind, account_state):
    item = SubElement(prtlist, "PRTLIST_ITEM", {"num": str(num)})
    add_text(item, "PAGENUM", page)
    participant = SubElement(item, "PARTICIPANT")
    add_text(participant, "PARTIC_KIND", participant_kind)
    if party.party_type.value == "LEGAL":
        j = SubElement(participant, "JURNAME")
        add_text(j, "ORGFORM_CODE", party.orgform_code)
        add_text(j, "ORGNAME", party.display_name)
        add_text(j, "BRANCH", "00")
    else:
        ph = SubElement(participant, "PHYSNAME")
        add_text(ph, "LASTNAME", party.last_name)
        add_text(ph, "FIRSTNAME", party.first_name)
        add_text(ph, "MIDDLENAME", party.middle_name)
    add_text(participant, "REGNUM", party.inn)
    if party.party_type.value == "LEGAL":
        add_text(participant, "OKPO", party.okpo)
        f = SubElement(participant, "FOREIGNER")
        add_text(f, "RESIDENT", party.resident_code)
        add_text(f, "REGNUM", party.registration_number)
        add_text(f, "ORGAN", party.registration_authority)
    else:
        add_text(participant, "NATION_CODE", party.country_code)
        o = SubElement(participant, "OWNER")
        add_text(o, "FLAG", "2")
        add_text(o, "OKPO", party.okpo)
    add_address(participant, "LEGAL_ADDRESS", party, "legal")
    add_address(participant, "NATURAL_ADDRESS", party, "actual")
    if party.party_type.value == "LEGAL":
        info = SubElement(participant, "ADDINFO")
        add_text(info, "ACTIVITY", party.activity)
        add_text(info, "ACTIVITIES", party.additional_activities)
        disponent = SubElement(participant, "DISPONENT")
        add_text(disponent, "NAME", party.authorized_person_name)
        dd = SubElement(disponent, "DOC")
        add_text(dd, "DOC_CODE", party.authorized_document_code)
        add_text(dd, "SERIES", party.authorized_document_series)
        add_text(dd, "NUM", party.authorized_document_number)
        add_text(dd, "ISSUEDATE", party.authorized_document_issue_date.strftime("%m/%d/%Y 0:0:0") if party.authorized_document_issue_date else "00")
        add_text(dd, "ORGAN", party.authorized_document_issuer)
    if party.party_type.value == "PHYSICAL":
        d = SubElement(participant, "DOC")
        add_text(d, "DOC_CODE", party.document_code)
        add_text(d, "SERIES", party.document_series)
        add_text(d, "NUM", party.document_number)
        add_text(d, "ISSUEDATE", party.document_issue_date.strftime("%m/%d/%Y 0:0:0") if party.document_issue_date else "00")
        add_text(d, "ORGAN", party.document_issuer)
        add_text(d, "BIRTHDATE", party.birth_date.strftime("%m/%d/%Y 0:0:0") if party.birth_date else "00")
        add_text(d, "BIRTHPLACE", party.birth_place)
    add_text(participant, "PAGENUM_REF", "00")
    acc = SubElement(item, "ACCOUNT")
    add_text(acc, "NUM", party.account_number)
    add_text(acc, "BANK", party.account_bank)
    add_text(acc, "BIC", party.account_bic)
    add_text(acc, "COUNTRY_CODE", party.account_country_code)
    add_text(acc, "STATE", account_state)
    add_text(acc, "ADDRESS", party.account_address)


def generate_xml(profile, reporting, trade) -> bytes:
    root = Element("ROWSET")
    row = SubElement(root, "ROW", {"num": "1"})
    msg = SubElement(row, "MSG")
    add_text(msg, "MSGNUM", reporting.message_number or 1)
    add_text(msg, "MSGDATE", datetime.utcnow().strftime("%m/%d/%Y 0:0:0"))
    add_text(msg, "PAGECOUNT", 3)
    add_text(msg, "MSGTYPE", reporting.message_type)
    person = SubElement(row, "PERSON")
    for tag, attr in [("ORG_KIND","org_kind"),("REGNUM","inn"),("BANK_BIC","bank_bic"),("OKPO","okpo"),("ORGFORM_CODE","orgform_code"),("PERSON_NAME","person_name"),("BRANCH","branch")]:
        add_text(person, tag, getattr(profile, attr))
    add_address(person, "LEGAL_ADDRESS", profile, "legal")
    add_address(person, "NATURAL_ADDRESS", profile, "legal")
    perf = SubElement(person, "PERFORMER")
    add_text(perf, "NAME", profile.performer_name); add_text(perf, "POST", profile.performer_post)
    add_text(person, "PHONE", profile.phone)
    rfq = trade.quote.rfq
    oper = SubElement(row, "OPER")
    add_text(oper, "OPERDATE", reporting.operation_date.strftime("%m/%d/%Y %H:%M:00"))
    add_text(oper, "OPER_CODE", reporting.operation_code)
    add_text(oper, "OPER_CODES", reporting.additional_operation_codes)
    add_text(oper, "SUMCUR", f"{Decimal(str(rfq.fiat_amount)):.2f}")
    add_text(oper, "CUR_CODES", reporting.currency_codes or CURRENCY_CODES.get(rfq.quote_asset, "00"))
    add_text(oper, "SUMTGT", f"{Decimal(str(rfq.amount)):.8f}".rstrip("0").rstrip("."))
    add_text(oper, "SUM", f"{Decimal(str(reporting.kgs_equivalent)):.2f}")
    add_text(oper, "SHARE_QTY", "00"); add_text(oper, "SHARE_CAPITAL", "00")
    add_text(oper, "REASON", reporting.reason)
    add_text(oper, "LIMIT_CODES", reporting.unusual_code)
    add_text(oper, "SHADY_CODES", reporting.unusual_codes)
    add_text(oper, "STATUS_CODE", reporting.operation_state)
    add_text(oper, "PRTCOUNT", 2)
    add_text(oper, "EXTRAINFO", reporting.extra_info)
    prt = SubElement(row, "PRTLIST")
    is_buy = rfq.side == "BUY"
    add_party(prt, 1, "002Ю" if reporting.exchange_party.party_type.value == "LEGAL" else "002Ф", reporting.exchange_party, reporting.exchange_participant_kind or ("05" if is_buy else "04"), "1" if is_buy else "2")
    add_party(prt, 2, "003Ю" if reporting.client_party.party_type.value == "LEGAL" else "003Ф", reporting.client_party, reporting.client_participant_kind or ("04" if is_buy else "05"), "2" if is_buy else "1")
    raw = tostring(root, encoding="windows-1251", xml_declaration=True)
    return raw

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class RFQStatus(str, enum.Enum):
    SUBMITTED = "SUBMITTED"
    QUOTED = "QUOTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class AmountType(str, enum.Enum):
    CRYPTO = "CRYPTO"
    FIAT = "FIAT"


class TradeStatus(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    FUNDED = "FUNDED"
    AML_REVIEW = "AML_REVIEW"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    SETTLED = "SETTLED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RFQ(Base):
    __tablename__ = "rfqs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_name: Mapped[str] = mapped_column(String(120))
    side: Mapped[str] = mapped_column(String(10))
    base_asset: Mapped[str] = mapped_column(String(16))
    quote_asset: Mapped[str] = mapped_column(String(16))
    network: Mapped[str | None] = mapped_column(String(40), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(30, 8), nullable=True)
    fiat_amount: Mapped[float | None] = mapped_column(Numeric(30, 8), nullable=True)
    amount_type: Mapped[AmountType] = mapped_column(Enum(AmountType), default=AmountType.CRYPTO)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[RFQStatus] = mapped_column(Enum(RFQStatus), default=RFQStatus.SUBMITTED)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    quote = relationship("Quote", back_populates="rfq", uselist=False, cascade="all, delete-orphan")


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("rfqs.id"), unique=True)
    price: Mapped[float] = mapped_column(Numeric(30, 8))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    dealer_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    rfq = relationship("RFQ", back_populates="quote")
    trade = relationship("Trade", back_populates="quote", uselist=False)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"), unique=True)
    status: Mapped[TradeStatus] = mapped_column(Enum(TradeStatus), default=TradeStatus.ACCEPTED)
    bank_fee: Mapped[float] = mapped_column(Numeric(30, 8), default=0)
    bank_fee_payer: Mapped[str] = mapped_column(String(20), default="CLIENT")
    network_fee: Mapped[float] = mapped_column(Numeric(30, 8), default=0)
    network_fee_payer: Mapped[str] = mapped_column(String(20), default="CLIENT")
    fees_included_in_quote: Mapped[bool] = mapped_column(Boolean, default=False)
    bank_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(180), nullable=True)
    aml_risk: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    archive_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    quote = relationship("Quote", back_populates="trade")
    history = relationship("TradeHistory", back_populates="trade", cascade="all, delete-orphan")


class TradeHistory(Base):
    __tablename__ = "trade_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"))
    old_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str] = mapped_column(String(32))
    changed_by: Mapped[str] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    trade = relationship("Trade", back_populates="history")


class QuoteAcceptance(Base):
    __tablename__ = "quote_acceptances"
    __table_args__ = (UniqueConstraint("idempotency_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(120))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class PartyType(str, enum.Enum):
    LEGAL = "LEGAL"
    PHYSICAL = "PHYSICAL"


class Party(Base):
    __tablename__ = "parties"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    party_type: Mapped[PartyType] = mapped_column(Enum(PartyType))
    party_type_code: Mapped[str] = mapped_column(String(3), default="002")
    display_name: Mapped[str] = mapped_column(String(254))
    inn: Mapped[str] = mapped_column(String(14), default="00")
    okpo: Mapped[str] = mapped_column(String(8), default="00")
    country_code: Mapped[str] = mapped_column(String(3), default="417")
    resident_code: Mapped[str] = mapped_column(String(1), default="1")
    orgform_code: Mapped[str] = mapped_column(String(2), default="20")
    registration_number: Mapped[str] = mapped_column(String(100), default="00")
    registration_authority: Mapped[str] = mapped_column(String(254), default="00")
    activity: Mapped[str] = mapped_column(String(254), default="00")
    additional_activities: Mapped[str] = mapped_column(String(254), default="00")
    authorized_person_name: Mapped[str] = mapped_column(String(100), default="00")
    authorized_document_code: Mapped[str] = mapped_column(String(3), default="00")
    authorized_document_series: Mapped[str] = mapped_column(String(10), default="00")
    authorized_document_number: Mapped[str] = mapped_column(String(20), default="00")
    authorized_document_issue_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    authorized_document_issuer: Mapped[str] = mapped_column(String(254), default="00")
    last_name: Mapped[str] = mapped_column(String(100), default="00")
    first_name: Mapped[str] = mapped_column(String(30), default="00")
    middle_name: Mapped[str] = mapped_column(String(30), default="00")
    document_code: Mapped[str] = mapped_column(String(3), default="00")
    document_series: Mapped[str] = mapped_column(String(10), default="00")
    document_number: Mapped[str] = mapped_column(String(20), default="00")
    document_issue_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    document_issuer: Mapped[str] = mapped_column(String(254), default="00")
    birth_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    birth_place: Mapped[str] = mapped_column(String(254), default="00")
    legal_postcode: Mapped[str] = mapped_column(String(6), default="00")
    legal_town_code: Mapped[str] = mapped_column(String(14), default="00")
    legal_region: Mapped[str] = mapped_column(String(100), default="00")
    legal_area: Mapped[str] = mapped_column(String(100), default="00")
    legal_town: Mapped[str] = mapped_column(String(100), default="00")
    legal_street: Mapped[str] = mapped_column(String(100), default="00")
    legal_house: Mapped[str] = mapped_column(String(4), default="00")
    legal_room: Mapped[str] = mapped_column(String(6), default="00")
    actual_postcode: Mapped[str] = mapped_column(String(6), default="00")
    actual_town_code: Mapped[str] = mapped_column(String(14), default="00")
    actual_region: Mapped[str] = mapped_column(String(100), default="00")
    actual_area: Mapped[str] = mapped_column(String(100), default="00")
    actual_town: Mapped[str] = mapped_column(String(100), default="00")
    actual_street: Mapped[str] = mapped_column(String(100), default="00")
    actual_house: Mapped[str] = mapped_column(String(4), default="00")
    actual_room: Mapped[str] = mapped_column(String(6), default="00")
    account_number: Mapped[str] = mapped_column(String(50), default="00")
    account_bank: Mapped[str] = mapped_column(String(254), default="00")
    account_bic: Mapped[str] = mapped_column(String(12), default="00")
    account_country_code: Mapped[str] = mapped_column(String(3), default="00")
    account_address: Mapped[str] = mapped_column(String(254), default="00")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReportingProfile(Base):
    __tablename__ = "reporting_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_kind: Mapped[str] = mapped_column(String(2), default="28")
    inn: Mapped[str] = mapped_column(String(14))
    bank_bic: Mapped[str] = mapped_column(String(6), default="00")
    okpo: Mapped[str] = mapped_column(String(8), default="00")
    orgform_code: Mapped[str] = mapped_column(String(2), default="20")
    person_name: Mapped[str] = mapped_column(String(254))
    branch: Mapped[str] = mapped_column(String(254), default="00")
    legal_postcode: Mapped[str] = mapped_column(String(6), default="00")
    legal_town_code: Mapped[str] = mapped_column(String(14), default="00")
    legal_region: Mapped[str] = mapped_column(String(100), default="00")
    legal_area: Mapped[str] = mapped_column(String(100), default="00")
    legal_town: Mapped[str] = mapped_column(String(100), default="00")
    legal_street: Mapped[str] = mapped_column(String(100), default="00")
    legal_house: Mapped[str] = mapped_column(String(4), default="00")
    legal_room: Mapped[str] = mapped_column(String(6), default="00")
    performer_name: Mapped[str] = mapped_column(String(100), default="00")
    performer_post: Mapped[str] = mapped_column(String(100), default="00")
    phone: Mapped[str] = mapped_column(String(12), default="00")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TradeReporting(Base):
    __tablename__ = "trade_reporting"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), unique=True)
    client_party_id: Mapped[int | None] = mapped_column(ForeignKey("parties.id"), nullable=True)
    exchange_party_id: Mapped[int | None] = mapped_column(ForeignKey("parties.id"), nullable=True)
    message_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_type: Mapped[str] = mapped_column(String(1), default="1")
    operation_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operation_code: Mapped[str] = mapped_column(String(6), default="8001")
    additional_operation_codes: Mapped[str] = mapped_column(String(29), default="00")
    currency_codes: Mapped[str] = mapped_column(String(27), default="00")
    client_participant_kind: Mapped[str] = mapped_column(String(2), default="05")
    exchange_participant_kind: Mapped[str] = mapped_column(String(2), default="04")
    kgs_equivalent: Mapped[float | None] = mapped_column(Numeric(30, 2), nullable=True)
    reason: Mapped[str] = mapped_column(String(254), default="00")
    unusual_code: Mapped[str] = mapped_column(String(4), default="00")
    unusual_codes: Mapped[str] = mapped_column(String(27), default="00")
    operation_state: Mapped[str] = mapped_column(String(1), default="1")
    extra_info: Mapped[str] = mapped_column(String(254), default="00")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    trade = relationship("Trade")
    client_party = relationship("Party", foreign_keys=[client_party_id])
    exchange_party = relationship("Party", foreign_keys=[exchange_party_id])

import enum
from datetime import datetime
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

class RFQStatus(str, enum.Enum):
    SUBMITTED = "SUBMITTED"
    QUOTED = "QUOTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

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
    amount: Mapped[float] = mapped_column(Numeric(30, 8))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[RFQStatus] = mapped_column(Enum(RFQStatus), default=RFQStatus.SUBMITTED)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    quote = relationship("Quote", back_populates="rfq", uselist=False, cascade="all, delete-orphan")

class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("rfqs.id"), unique=True)
    price: Mapped[float] = mapped_column(Numeric(30, 8))
    fee_rate: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
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

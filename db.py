"""Modèles SQLite (SQLAlchemy 2.0) et helpers d'accès à la base.

Tables :
- products  : un produit détecté par une source (Shopify, Amazon, import pubs)
- analyses  : résultats de l'analyse IA (cache des appels Claude)
- tests     : suivi des produits testés en boutique (budget, CA, statut)
- runs      : journal des collectes (pour afficher les erreurs de sources)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterator

from contextlib import contextmanager
from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from settings import db_path

TEST_STATUSES = ["à tester", "en test", "validé", "abandonné"]


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_source_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(30))          # shopify | amazon | ads
    source_id: Mapped[str] = mapped_column(String(255))      # id unique dans la source
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(1000))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    store: Mapped[str | None] = mapped_column(String(255))   # domaine boutique / "amazon.fr"
    category: Mapped[str | None] = mapped_column(String(255))
    vendor: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price_eur: Mapped[float | None] = mapped_column(Float)
    supplier_cost_eur: Mapped[float | None] = mapped_column(Float)   # prix d'achat (saisi / importé)
    supplier_url: Mapped[str | None] = mapped_column(String(1000))
    weight_grams: Mapped[float | None] = mapped_column(Float)
    best_seller_rank: Mapped[int | None] = mapped_column(Integer)

    # Données publicitaires (import Minea / PPSPY / Meta Ad Library)
    advertisers_count: Mapped[int | None] = mapped_column(Integer)
    ad_first_seen: Mapped[datetime | None] = mapped_column(DateTime)
    ad_last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    ad_links: Mapped[str | None] = mapped_column(Text)        # JSON list[str]

    # Google Trends
    keyword: Mapped[str | None] = mapped_column(String(255))  # mot-clé utilisé pour Trends
    trend_mean: Mapped[float | None] = mapped_column(Float)   # moyenne récente 0..100
    trend_slope: Mapped[float | None] = mapped_column(Float)  # pente normalisée -1..1
    trend_data: Mapped[str | None] = mapped_column(Text)      # JSON {date: valeur}
    trend_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Résultats calculés
    saturation_stores: Mapped[int | None] = mapped_column(Integer)
    wow_score: Mapped[float | None] = mapped_column(Float)     # 0..10, évalué par Claude
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusion_reasons: Mapped[str | None] = mapped_column(Text)  # JSON list[str]
    score: Mapped[float | None] = mapped_column(Float)
    score_details: Mapped[str | None] = mapped_column(Text)    # JSON {critère: {...}}

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    analyses: Mapped[list["Analysis"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    tests: Mapped[list["TestRecord"]] = relationship(back_populates="product", cascade="all, delete-orphan")

    # --- helpers JSON ---
    def json_field(self, name: str, default: Any = None) -> Any:
        raw = getattr(self, name)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    model: Mapped[str] = mapped_column(String(100))
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped[Product] = relationship(back_populates="analyses")

    @property
    def result(self) -> dict:
        return json.loads(self.result_json)


class TestRecord(Base):
    __tablename__ = "tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    status: Mapped[str] = mapped_column(String(30), default="à tester")
    budget_spent: Mapped[float] = mapped_column(Float, default=0.0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product: Mapped[Product] = relationship(back_populates="tests")

    @property
    def roas(self) -> float | None:
        return round(self.revenue / self.budget_spent, 2) if self.budget_spent else None


class Run(Base):
    """Journal d'une collecte : quelles sources ont réussi / échoué."""
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    report_json: Mapped[str] = mapped_column(Text, default="{}")


# ---------------------------------------------------------------------
# Moteur / sessions
# ---------------------------------------------------------------------
_path = db_path()
_path.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{_path}", future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def get_session() -> Iterator[Session]:
    """Session transactionnelle : commit si tout va bien, rollback sinon."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Colonnes qu'un collecteur a le droit d'écraser lors d'un upsert.
_UPSERT_FIELDS = {
    "title", "url", "image_url", "store", "category", "vendor", "tags", "description",
    "price_eur", "supplier_cost_eur", "supplier_url", "weight_grams", "best_seller_rank",
    "advertisers_count", "ad_first_seen", "ad_last_seen", "ad_links", "keyword",
}


def upsert_product(session: Session, data: dict) -> Product:
    """Insère ou met à jour un produit identifié par (source, source_id).

    Les valeurs None ne remplacent pas une valeur existante (pour ne pas
    effacer une saisie manuelle, ex. prix d'achat).
    """
    prod = session.scalar(
        select(Product).where(Product.source == data["source"], Product.source_id == str(data["source_id"]))
    )
    if prod is None:
        prod = Product(source=data["source"], source_id=str(data["source_id"]), title=data.get("title") or "?")
        session.add(prod)
    for key, value in data.items():
        if key in _UPSERT_FIELDS and value is not None:
            if key == "ad_links" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            # Un mot-clé déjà défini (éventuellement corrigé à la main) est conservé.
            if key == "keyword" and prod.keyword:
                continue
            setattr(prod, key, value)
    return prod


init_db()

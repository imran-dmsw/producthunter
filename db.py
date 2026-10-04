"""Modèles SQLite (SQLAlchemy 2.0) et helpers d'accès à la base.

Tables :
- products        : un produit détecté (boutique Shopify concurrente ou import pubs).
                    Les produits identiques vendus par plusieurs boutiques sont
                    regroupés : `cluster_id` pointe vers le produit « représentant ».
- supplier_offers : offres fournisseur trouvées (CJ Dropshipping, AliExpress, CSV, saisie)
- api_cache       : cache des réponses d'API (évite de rappeler CJ / AliExpress)
- analyses        : contenus générés par Claude (cache)
- tests           : suivi des produits testés en boutique (budget, CA, statut)
- runs            : journal des collectes
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine, inspect, select, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from settings import db_path

TEST_STATUSES = ["à tester", "en test", "validé", "abandonné"]

# Statut de la recherche fournisseur
SUPPLIER_PENDING = "non cherché"
SUPPLIER_FOUND = "trouvé"
SUPPLIER_NOT_FOUND = "fournisseur introuvable"


class Base(DeclarativeBase):
    pass


class JsonMixin:
    def json_field(self, name: str, default: Any = None) -> Any:
        raw = getattr(self, name)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default


class Product(JsonMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_source_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(30))          # shopify | ads
    source_id: Mapped[str] = mapped_column(String(255))      # id unique dans la source
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(1000))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    store: Mapped[str | None] = mapped_column(String(255))   # domaine de la boutique concurrente
    category: Mapped[str | None] = mapped_column(String(255))
    vendor: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price_eur: Mapped[float | None] = mapped_column(Float)   # prix de vente observé chez le concurrent
    weight_grams: Mapped[float | None] = mapped_column(Float)
    best_seller_rank: Mapped[int | None] = mapped_column(Integer)

    # Ancien champ (v1) conservé pour compatibilité ; remplacé par supplier_offers
    supplier_cost_eur: Mapped[float | None] = mapped_column(Float)
    supplier_url: Mapped[str | None] = mapped_column(String(1000))

    # Données publicitaires (import Minea / PPSPY / Meta Ad Library)
    advertisers_count: Mapped[int | None] = mapped_column(Integer)
    ad_first_seen: Mapped[datetime | None] = mapped_column(DateTime)
    ad_last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    ad_links: Mapped[str | None] = mapped_column(Text)        # JSON list[str]

    # Google Trends
    keyword: Mapped[str | None] = mapped_column(String(255))  # mot-clé (FR) pour Trends
    trend_mean: Mapped[float | None] = mapped_column(Float)
    trend_slope: Mapped[float | None] = mapped_column(Float)
    trend_data: Mapped[str | None] = mapped_column(Text)
    trend_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Regroupement des concurrents
    cluster_id: Mapped[int | None] = mapped_column(Integer, index=True)   # id du représentant
    competitors: Mapped[str | None] = mapped_column(Text)     # JSON [{store, url, price, title}]
    saturation_stores: Mapped[int | None] = mapped_column(Integer)

    # Fournisseur
    search_query: Mapped[str | None] = mapped_column(String(255))     # requête (EN) envoyée aux fournisseurs
    supplier_status: Mapped[str] = mapped_column(String(40), default=SUPPLIER_PENDING)
    supplier_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    selected_offer_id: Mapped[int | None] = mapped_column(Integer)
    offer_locked: Mapped[bool] = mapped_column(Boolean, default=False)   # offre choisie à la main

    # Économie unitaire (calculée depuis l'offre retenue)
    recommended_price: Mapped[float | None] = mapped_column(Float)
    landed_cost: Mapped[float | None] = mapped_column(Float)          # achat + livraison
    net_profit: Mapped[float | None] = mapped_column(Float)
    net_margin_pct: Mapped[float | None] = mapped_column(Float)

    # Résultats
    wow_score: Mapped[float | None] = mapped_column(Float)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusion_reasons: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    score_details: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    analyses: Mapped[list["Analysis"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    tests: Mapped[list["TestRecord"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    offers: Mapped[list["SupplierOffer"]] = relationship(back_populates="product", cascade="all, delete-orphan")

    @property
    def is_representative(self) -> bool:
        return self.cluster_id is None or self.cluster_id == self.id

    def selected_offer(self) -> "SupplierOffer | None":
        return next((o for o in self.offers if o.id == self.selected_offer_id), None)


class SupplierOffer(JsonMixin, Base):
    """Une offre fournisseur réelle pour un produit (jamais estimée)."""
    __tablename__ = "supplier_offers"
    __table_args__ = (UniqueConstraint("product_id", "supplier", "supplier_product_id", name="uq_offer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    supplier: Mapped[str] = mapped_column(String(30))            # cj | aliexpress | csv | manuel
    supplier_product_id: Mapped[str] = mapped_column(String(100))
    variant_id: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(1000))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    price_eur: Mapped[float | None] = mapped_column(Float)          # prix d'achat unitaire
    shipping_eur: Mapped[float | None] = mapped_column(Float)       # livraison vers la France
    shipping_method: Mapped[str | None] = mapped_column(String(120))
    delivery_days_min: Mapped[int | None] = mapped_column(Integer)
    delivery_days_max: Mapped[int | None] = mapped_column(Integer)
    stock: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[float | None] = mapped_column(Float)            # note /5
    orders: Mapped[int | None] = mapped_column(Integer)            # ventes / commandes constatées
    weight_grams: Mapped[float | None] = mapped_column(Float)
    match_score: Mapped[float | None] = mapped_column(Float)       # similarité avec le produit détecté (0..1)
    raw_json: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped[Product] = relationship(back_populates="offers")

    @property
    def landed_cost(self) -> float | None:
        if self.price_eur is None or self.shipping_eur is None:
            return None
        return round(self.price_eur + self.shipping_eur, 2)


class ApiCache(Base):
    __tablename__ = "api_cache"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    response_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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


def _migrate() -> None:
    """Ajoute les colonnes manquantes aux tables existantes (bases créées en v1)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    coltype = col.type.compile(engine.dialect)
                    default = ""
                    if col.default is not None and not callable(col.default.arg):
                        val = col.default.arg
                        default = f" DEFAULT {int(val) if isinstance(val, bool) else repr(val)}"
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {coltype}{default}'))


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate()


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


# ---------------------------------------------------------------------
# Cache d'API
# ---------------------------------------------------------------------
def cache_get(key: str, max_age_hours: float) -> Any | None:
    with get_session() as s:
        row = s.get(ApiCache, key)
        if row and row.fetched_at >= datetime.utcnow() - timedelta(hours=max_age_hours):
            return json.loads(row.response_json)
    return None


def cache_set(key: str, value: Any) -> None:
    with get_session() as s:
        row = s.get(ApiCache, key) or ApiCache(key=key)
        row.response_json = json.dumps(value, ensure_ascii=False, default=str)
        row.fetched_at = datetime.utcnow()
        s.merge(row)


# ---------------------------------------------------------------------
# Upsert produits
# ---------------------------------------------------------------------
_UPSERT_FIELDS = {
    "title", "url", "image_url", "store", "category", "vendor", "tags", "description",
    "price_eur", "weight_grams", "best_seller_rank",
    "advertisers_count", "ad_first_seen", "ad_last_seen", "ad_links", "keyword",
}


def upsert_product(session: Session, data: dict) -> Product:
    """Insère ou met à jour un produit identifié par (source, source_id).

    Les valeurs None n'écrasent pas une valeur existante, et un mot-clé déjà
    défini (éventuellement corrigé à la main) est conservé.
    """
    prod = session.scalar(
        select(Product).where(Product.source == data["source"], Product.source_id == str(data["source_id"]))
    )
    if prod is None:
        prod = Product(source=data["source"], source_id=str(data["source_id"]), title=data.get("title") or "?",
                       supplier_status=SUPPLIER_PENDING)
        session.add(prod)
    for key, value in data.items():
        if key in _UPSERT_FIELDS and value is not None:
            if key == "ad_links" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            if key == "keyword" and prod.keyword:
                continue
            setattr(prod, key, value)
    return prod


def upsert_offer(session: Session, product_id: int, data: dict) -> SupplierOffer:
    """Insère ou met à jour une offre (product_id, supplier, supplier_product_id)."""
    offer = session.scalar(select(SupplierOffer).where(
        SupplierOffer.product_id == product_id, SupplierOffer.supplier == data["supplier"],
        SupplierOffer.supplier_product_id == str(data["supplier_product_id"])))
    if offer is None:
        offer = SupplierOffer(product_id=product_id, supplier=data["supplier"],
                              supplier_product_id=str(data["supplier_product_id"]), title=data.get("title") or "?")
        session.add(offer)
    for key, value in data.items():
        if key in ("supplier", "supplier_product_id"):
            continue
        if key == "raw":
            offer.raw_json = json.dumps(value, ensure_ascii=False, default=str)[:20000]
        elif hasattr(SupplierOffer, key):
            setattr(offer, key, value)
    offer.fetched_at = datetime.utcnow()
    return offer


init_db()

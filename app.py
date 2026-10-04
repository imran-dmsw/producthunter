"""Interface Streamlit de product-hunter.

Lancement (depuis ce dossier, pour appliquer le thème) :  streamlit run app.py
Pages : Produits · Fiche produit · Mes tests · Réglages
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import analyzer
import shopify_export
from collectors import google_trends
from db import (SUPPLIER_FOUND, SUPPLIER_NOT_FOUND, SUPPLIER_PENDING, TEST_STATUSES, Product, Run,
                SupplierOffer, TestRecord, get_session, upsert_offer)
from pipeline import run_collection
from scoring import CRITERIA_LABELS, offer_exclusions, rescore_all
from settings import API_KEYS, load_config, masked_key, save_config, set_api_key
from suppliers import matching

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
st.set_page_config(page_title="Product Hunter", page_icon=":material/insights:", layout="wide")

PAGES = ["Produits", "Fiche produit", "Mes tests", "Réglages"]
PAGE_ICONS = [":material/storefront:", ":material/inventory_2:", ":material/monitoring:", ":material/tune:"]

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; max-width: 1400px;}
  .ph-header {border-bottom: 1px solid #E2E8F0; padding-bottom: .8rem; margin-bottom: 1.2rem;}
  .ph-header h1 {font-size: 1.75rem; margin: 0; padding: 0; letter-spacing: -0.02em;}
  .ph-header p {color: #64748B; margin: .25rem 0 0 0; font-size: .95rem;}
  .ph-brand {font-weight: 700; font-size: 1.15rem; letter-spacing: .02em; color: #F8FAFC;}
  .ph-brand span {color: #34D399;}
  .ph-tag {color: #94A3B8; font-size: .78rem; text-transform: uppercase; letter-spacing: .08em;}
  .ph-card-title {font-weight: 600; font-size: .95rem; line-height: 1.25; height: 2.5em; overflow: hidden;}
  .ph-kv {display: flex; justify-content: space-between; font-size: .85rem; padding: 2px 0;
          border-bottom: 1px dashed #EEF1F5;}
  .ph-kv span:first-child {color: #64748B;}
  .ph-kv span:last-child {font-weight: 600;}
  .ph-profit {color: #0E7C66;}
  .ph-badge {display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: .75rem; font-weight: 600;
             margin: 0 4px 4px 0;}
  [data-testid="stMetricValue"] {font-weight: 600;}
</style>
""", unsafe_allow_html=True)


def page_header(title: str, subtitle: str) -> None:
    st.markdown(f'<div class="ph-header"><h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


def badge(text: str, color: str) -> str:
    colors = {"green": ("#DCFCE7", "#166534"), "red": ("#FEE2E2", "#991B1B"),
              "amber": ("#FEF3C7", "#92400E"), "grey": ("#F1F5F9", "#475569")}
    bg, fg = colors[color]
    return f'<span class="ph-badge" style="background:{bg};color:{fg}">{text}</span>'


def status_badge(row) -> str:
    if row["fournisseur"] == SUPPLIER_NOT_FOUND:
        return badge("fournisseur introuvable", "red")
    if row["fournisseur"] == SUPPLIER_PENDING and not row["offres"]:
        return badge("fournisseur non cherché", "grey")
    if row["exclu"]:
        return badge("exclu", "amber")
    return badge("prêt", "green")


# =====================================================================
# Données
# =====================================================================
def load_products_df() -> pd.DataFrame:
    """Une ligne par produit représentant (les doublons de concurrents sont regroupés)."""
    rows = []
    with get_session() as s:
        products = s.scalars(select(Product).options(selectinload(Product.offers))).all()
        for p in products:
            if not p.is_representative:
                continue
            o = p.selected_offer()
            rows.append({
                "id": p.id,
                "image": (o.image_url if o and o.image_url else p.image_url) or "",
                "titre": p.title,
                "score": p.score,
                "achat €": o.price_eur if o else None,
                "port €": o.shipping_eur if o else None,
                "prix conseillé €": p.recommended_price,
                "profit net €": p.net_profit,
                "marge %": p.net_margin_pct,
                "délai (j)": f"{o.delivery_days_min or '?'}-{o.delivery_days_max}" if o and o.delivery_days_max else None,
                "délai max": o.delivery_days_max if o else None,
                "fournisseur_nom": o.supplier if o else None,
                "lien fournisseur": o.url if o else None,
                "concurrents": len(p.json_field("competitors", [])),
                "annonceurs": p.advertisers_count,
                "catégorie": p.category or "—",
                "fournisseur": p.supplier_status,
                "offres": len(p.offers),
                "exclu": p.excluded,
                "raisons": ", ".join(p.json_field("exclusion_reasons", [])),
            })
    return pd.DataFrame(rows)


def go_to_product(pid: int) -> None:
    st.session_state["product_id"] = pid
    st.session_state["page"] = PAGES[1]


def show_report(report: dict) -> None:
    for src, rep in report.items():
        if src == "scoring":
            st.write(f"**Score** : {rep}")
            continue
        dot = ":green[●]" if not rep.get("errors") else (":orange[●]" if rep.get("items") else ":red[●]")
        st.write(f"{dot} **{src}** — {rep.get('items', 0)} élément(s)")
        for e in rep.get("errors", [])[:8]:
            st.caption(f":red[{e}]")
        for i in rep.get("info", [])[:8]:
            st.caption(f"· {i}")


def export_panel(ids: list[int], cfg: dict, key: str) -> None:
    """Bouton « Exporter vers Shopify » pour une sélection de produits."""
    if not ids:
        st.caption("Sélectionnez des produits pour les exporter vers Shopify.")
        return
    data, warnings, n = shopify_export.export_csv(ids, cfg)
    st.download_button(f"Exporter vers Shopify ({n} produit{'s' if n > 1 else ''})", data,
                       file_name=f"shopify_import_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv",
                       type="primary", icon=":material/upload_file:", disabled=n == 0, key=key)
    for w in warnings:
        st.caption(f":orange[{w}]")


def _kv(k, v, cls=""):
    return f'<div class="ph-kv"><span>{k}</span><span class="{cls}">{v}</span></div>'


def _eur(x) -> str:
    return "—" if x is None or pd.isna(x) else f"{x:.2f} €"


# =====================================================================
# Page — Produits
# =====================================================================
def page_products() -> None:
    page_header("Produits", "Produits niche détectés, avec fournisseur, marge réelle et prix conseillé")
    cfg = load_config()

    with st.expander("Lancer une collecte", icon=":material/sync:"):
        c1, c2 = st.columns([2, 3])
        with c1:
            src = {
                "shopify": st.checkbox("Boutiques Shopify concurrentes", value=cfg["shopify"].get("enabled", True)),
                "ads": st.checkbox("Import CSV pubs (Minea / PPSPY / Meta)", value=True),
                "trends": st.checkbox("Google Trends (lent)", value=cfg["google_trends"].get("enabled", True)),
                "suppliers": st.checkbox("Recherche fournisseurs (CJ, AliExpress)", value=True),
            }
            if not masked_key("CJ_API_KEY") and not masked_key("ALIEXPRESS_APP_KEY"):
                st.caption(":orange[Aucune clé fournisseur : renseignez CJ et/ou AliExpress dans Réglages → Clés API.]")
        with c2:
            ads_files = st.file_uploader("CSV de pubs (Minea, PPSPY, Meta Ad Library)", type=["csv"],
                                         accept_multiple_files=True)
            sup_files = st.file_uploader("CSV fournisseurs (AliExpress, DSers, agent de sourcing…)", type=["csv"],
                                         accept_multiple_files=True)
        if st.button("Lancer la collecte", type="primary", icon=":material/play_arrow:"):
            bar = st.progress(0.0, text="Démarrage…")
            with st.status("Collecte en cours…", expanded=True) as status:
                report = run_collection(
                    cfg, sources=tuple(k for k, v in src.items() if v),
                    uploaded_csvs=[(f.name, f.getvalue()) for f in ads_files or []],
                    supplier_csvs=[(f.name, f.getvalue()) for f in sup_files or []],
                    progress=lambda msg, pct: bar.progress(min(pct, 1.0), text=msg),
                )
                show_report(report)
                status.update(label="Collecte terminée", state="complete")

    with get_session() as s:
        run = s.scalar(select(Run).order_by(Run.started_at.desc()))
        last = (run.started_at, json.loads(run.report_json)) if run else None
    if last:
        with st.expander(f"Dernière collecte : {last[0]:%d/%m/%Y %H:%M} UTC", icon=":material/history:"):
            show_report(last[1])

    df = load_products_df()
    if df.empty:
        st.info("Aucun produit. Ajoutez vos boutiques concurrentes (Réglages) puis lancez une collecte.")
        return

    ready = df[~df["exclu"]]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Produits détectés", len(df), border=True)
    k2.metric("Avec offre fournisseur", int((df["offres"] > 0).sum()), border=True)
    k3.metric("Prêts à tester", len(ready), border=True)
    k4.metric("Meilleur profit net", _eur(ready["profit net €"].max()) if not ready.empty else "—", border=True)

    # ---- Filtres ----
    with st.sidebar:
        st.subheader("Filtres")
        view_mode = st.segmented_control("Affichage", ["Cartes", "Tableau"], default="Cartes") or "Cartes"
        scope = st.selectbox("Produits", ["Prêts à tester", "Tous", "Avec offre fournisseur (exclus inclus)",
                                          "Fournisseur introuvable", "Fournisseur non cherché"])
        search = st.text_input("Recherche")
        min_profit = st.number_input("Profit net min (€)", 0.0, 500.0, 0.0, 1.0)
        max_days = st.slider("Livraison max (jours)", 3, 30, int(cfg["filters"]["max_delivery_days"]))
        min_score = st.slider("Score minimum", 0, 100, 0)
        cats = st.multiselect("Catégorie", sorted(df["catégorie"].unique()))

    view = {
        "Prêts à tester": ready,
        "Tous": df,
        "Avec offre fournisseur (exclus inclus)": df[df["offres"] > 0],
        "Fournisseur introuvable": df[df["fournisseur"] == SUPPLIER_NOT_FOUND],
        "Fournisseur non cherché": df[(df["fournisseur"] == SUPPLIER_PENDING) & (df["offres"] == 0)],
    }[scope]
    view = view[view["score"].fillna(0) >= min_score]
    if min_profit:
        view = view[view["profit net €"].fillna(-1) >= min_profit]
    view = view[view["délai max"].isna() | (view["délai max"] <= max_days)]
    if cats:
        view = view[view["catégorie"].isin(cats)]
    if search:
        view = view[view["titre"].str.contains(search, case=False, na=False)]
    view = view.sort_values(["score", "profit net €"], ascending=False, na_position="last")

    # ---- Génération IA ----
    c1, c2, c3 = st.columns([2, 1, 3])
    top_n = c2.number_input("Top N", 1, 50, int(cfg["analyzer"].get("top_n", 10)), label_visibility="collapsed")
    if c1.button(f"Générer les fiches IA du top {top_n}", icon=":material/auto_awesome:",
                 disabled=not analyzer.has_api_key()):
        bar = st.progress(0.0)
        with st.spinner("Génération des fiches (mises en cache)…"):
            res = analyzer.generate_top(cfg, top_n, progress=lambda m, p: bar.progress(p, text=m))
        bar.empty()
        st.success(f"{res['générés']} fiche(s) prête(s), dont {res['depuis le cache']} depuis le cache.")
        for e in res["erreurs"]:
            st.warning(e)
    if not analyzer.has_api_key():
        c3.caption(":material/info: Génération IA désactivée : clé Anthropic absente (Réglages → Clés API)")

    st.caption(f"{len(view)} produit(s)")
    if view.empty and scope == "Prêts à tester":
        st.info("Aucun produit prêt : il faut une offre fournisseur conforme (prix, port et délai France réels). "
                "Configurez CJ / AliExpress dans Réglages → Clés API, importez un CSV fournisseur, "
                "ou complétez une offre dans la fiche produit. Le filtre « Produits » montre les autres statuts.")
    selected: list[int] = []

    if view_mode == "Tableau":
        cols = ["image", "titre", "score", "achat €", "port €", "prix conseillé €", "profit net €", "marge %",
                "délai (j)", "fournisseur_nom", "concurrents", "lien fournisseur"]
        if scope != "Prêts à tester":
            cols += ["fournisseur", "raisons"]
        event = st.dataframe(
            view[cols], hide_index=True, width="stretch", height=560, on_select="rerun",
            selection_mode="multi-row",
            column_config={
                "image": st.column_config.ImageColumn("", width="small"),
                "titre": st.column_config.TextColumn(width="large"),
                "score": st.column_config.ProgressColumn("score", min_value=0, max_value=100, format="%.0f"),
                "achat €": st.column_config.NumberColumn(format="%.2f €"),
                "port €": st.column_config.NumberColumn(format="%.2f €"),
                "prix conseillé €": st.column_config.NumberColumn(format="%.2f €"),
                "profit net €": st.column_config.NumberColumn(format="%.2f €"),
                "marge %": st.column_config.NumberColumn(format="%.0f %%"),
                "fournisseur_nom": st.column_config.TextColumn("fournisseur"),
                "lien fournisseur": st.column_config.LinkColumn(display_text="voir"),
            },
        )
        rows = event.selection.rows if event and event.selection else []
        selected = [int(view.iloc[r]["id"]) for r in rows]
        if len(selected) == 1:
            st.button("Ouvrir la fiche produit", icon=":material/open_in_new:", on_click=go_to_product,
                      args=(selected[0],))
    else:
        page_size = 24
        pages = max((len(view) - 1) // page_size + 1, 1)
        page = st.number_input(f"Page (sur {pages})", 1, pages, 1) if pages > 1 else 1
        chunk = view.iloc[(page - 1) * page_size: page * page_size]
        for start in range(0, len(chunk), 3):
            cols = st.columns(3)
            for col, (_, r) in zip(cols, chunk.iloc[start:start + 3].iterrows()):
                with col.container(border=True):
                    if r["image"]:
                        st.image(r["image"], width="stretch")
                    st.markdown(f'<div class="ph-card-title">{r["titre"]}</div>', unsafe_allow_html=True)
                    st.markdown(status_badge(r) + badge(f"score {r['score'] or 0:.0f}", "grey"),
                                unsafe_allow_html=True)
                    delay = f"{r['délai (j)']} j" if r["délai (j)"] else "—"
                    st.markdown(
                        _kv("Achat + port", f"{_eur(r['achat €'])} + {_eur(r['port €'])}")
                        + _kv("Prix conseillé", _eur(r["prix conseillé €"]))
                        + _kv("Profit net / commande", _eur(r["profit net €"]), "ph-profit")
                        + _kv("Livraison France", delay)
                        + _kv("Concurrents", r["concurrents"]),
                        unsafe_allow_html=True)
                    st.write("")
                    b1, b2 = st.columns(2)
                    if r["lien fournisseur"]:
                        b1.link_button("Fournisseur", r["lien fournisseur"], icon=":material/local_shipping:",
                                       width="stretch")
                    b2.button("Fiche", key=f"open_{r['id']}", icon=":material/open_in_new:", width="stretch",
                              on_click=go_to_product, args=(int(r["id"]),))
                    if r["prix conseillé €"] is not None and not pd.isna(r["prix conseillé €"]):
                        if st.checkbox("Sélectionner pour l'export", key=f"sel_{r['id']}"):
                            selected.append(int(r["id"]))

    st.divider()
    c1, c2 = st.columns([2, 1])
    with c1:
        export_panel(selected, cfg, key="export_main")
    c2.download_button("Export CSV (analyse)", view.drop(columns=["image"]).to_csv(index=False, sep=";")
                       .encode("utf-8-sig"), file_name=f"product-hunter_{datetime.now():%Y%m%d_%H%M}.csv",
                       mime="text/csv", icon=":material/download:")


# =====================================================================
# Page — Fiche produit
# =====================================================================
def page_product() -> None:
    page_header("Fiche produit", "Fournisseur, économie unitaire, concurrence et contenu Shopify")
    cfg = load_config()
    df = load_products_df()
    if df.empty:
        st.info("Aucun produit en base.")
        return
    df = df.sort_values(["exclu", "score"], ascending=[True, False], na_position="last")
    ids = df["id"].tolist()
    labels = {r.id: f"{r.score or 0:.0f} · {r.titre[:90]}" for r in df.itertuples()}
    current = st.session_state.get("product_id")
    pid = st.selectbox("Produit", ids, index=ids.index(current) if current in ids else 0,
                       format_func=lambda i: labels[i])
    st.session_state["product_id"] = pid

    with get_session() as s:
        p = s.get(Product, pid)
        prod = {c.name: getattr(p, c.name) for c in Product.__table__.columns}
        details = p.json_field("score_details", {})
        econ = details.get("_economics")
        competitors = p.json_field("competitors", [])
        reasons = p.json_field("exclusion_reasons", [])
        offers = [{c.name: getattr(o, c.name) for c in SupplierOffer.__table__.columns}
                  | {"landed": o.landed_cost, "problèmes": ", ".join(offer_exclusions(o, cfg))} for o in p.offers]
        trend = p.json_field("trend_data", {})
        ad_links = details.get("_ad_links") or p.json_field("ad_links", [])
    offer = next((o for o in offers if o["id"] == prod["selected_offer_id"]), None)

    # ---- En-tête ----
    c1, c2, c3 = st.columns([1.1, 2, 1.2])
    with c1:
        img = (offer or {}).get("image_url") or prod["image_url"]
        if img:
            st.image(img, width="stretch",
                     caption="Photo fournisseur" if offer and offer["image_url"] else "Photo concurrent")
    with c2:
        st.subheader(prod["title"])
        if reasons:
            st.markdown("".join(badge(r, "red" if "introuvable" in r else "amber") for r in reasons),
                        unsafe_allow_html=True)
        else:
            st.markdown(badge("prêt à tester", "green"), unsafe_allow_html=True)
        st.write("")
        if offer:
            st.markdown(f"**Fournisseur retenu : {offer['supplier'].upper()}** — {offer['title'][:120]}")
            st.markdown(
                _kv("Achat", _eur(offer["price_eur"])) + _kv("Livraison France", _eur(offer["shipping_eur"]))
                + _kv("Délai", f"{offer['delivery_days_min'] or '?'}-{offer['delivery_days_max'] or '?'} j"
                      + (f" ({offer['shipping_method']})" if offer["shipping_method"] else ""))
                + _kv("Stock", offer["stock"] if offer["stock"] is not None else "n/c")
                + _kv("Note", f"{offer['rating']:.1f}/5" if offer["rating"] else "n/c"),
                unsafe_allow_html=True)
            st.write("")
            if offer["url"]:
                st.link_button("Voir le fournisseur", offer["url"], icon=":material/local_shipping:", type="primary")
        if prod["url"]:
            st.link_button("Produit chez le concurrent", prod["url"], icon=":material/storefront:")
    with c3:
        st.metric("Score", f"{prod['score'] or 0:.0f}/100", border=True)
        st.metric("Prix conseillé", _eur(prod["recommended_price"]), border=True)
        st.metric("Profit net / commande", _eur(prod["net_profit"]),
                  f"{prod['net_margin_pct']:.0f} % du prix" if prod["net_margin_pct"] is not None else None,
                  border=True)
        if st.button("Ajouter à mes tests", icon=":material/add:", width="stretch"):
            with get_session() as s:
                exists = s.scalar(select(TestRecord).where(TestRecord.product_id == pid))
                if not exists:
                    s.add(TestRecord(product_id=pid, status="à tester"))
            st.toast("Ajouté à « Mes tests »" if not exists else "Déjà dans « Mes tests »")
        if prod["recommended_price"] is not None:
            export_panel([pid], cfg, key="export_one")

    tab_eco, tab_sup, tab_comp, tab_dem, tab_ai = st.tabs(
        ["Économie unitaire", f"Fournisseurs ({len(offers)})", f"Concurrents & pubs ({len(competitors)})",
         "Demande & score", "Fiche IA"])

    # ---- Économie ----
    with tab_eco:
        if econ and offer:
            e1, e2 = st.columns(2)
            rows = [("Prix de vente conseillé", econ["recommended_price"]),
                    ("Prix d'achat", -(offer["price_eur"] or 0)),
                    ("Livraison France", -(offer["shipping_eur"] or 0)),
                    (f"Frais de paiement ({cfg['economics']['payment_fees_pct']:.0f} %)", -econ["payment_fees"]),
                    (f"Pub estimée ({cfg['economics']['ad_cost_pct']:.0f} %)", -econ["ad_cost"]),
                    ("Profit net par commande", econ["net_profit"])]
            with e1:
                st.dataframe(pd.DataFrame(rows, columns=["poste", "€"]), hide_index=True, width="stretch",
                             column_config={"€": st.column_config.NumberColumn(format="%.2f €")})
                st.caption(f"Prix conseillé : {econ['basis']}. Plancher pour "
                           f"{cfg['economics']['target_net_margin_pct']:.0f} % de marge nette : {econ['floor_price']:.2f} €"
                           + (f" · médiane concurrents : {econ['market_price']:.2f} €" if econ["market_price"] else ""))
            with e2:
                fig = go.Figure(go.Waterfall(
                    orientation="v", measure=["absolute", "relative", "relative", "relative", "relative", "total"],
                    x=["Prix", "Achat", "Port", "Paiement", "Pub", "Profit"], y=[r[1] for r in rows[:-1]] + [0],
                    connector={"line": {"color": "#CBD5E1"}}, increasing={"marker": {"color": "#0E7C66"}},
                    decreasing={"marker": {"color": "#94A3B8"}}, totals={"marker": {"color": "#0E7C66"}}))
                fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0), showlegend=False)
                st.plotly_chart(fig, width="stretch")
        else:
            st.info("Pas d'offre fournisseur conforme : aucune marge n'est calculée (aucune donnée estimée).")

    # ---- Fournisseurs ----
    with tab_sup:
        q1, q2 = st.columns([3, 1])
        new_query = q1.text_input("Requête de recherche fournisseur (anglais)", prod["search_query"] or "")
        q2.write("")
        if q2.button("Rechercher les fournisseurs", icon=":material/search:", width="stretch"):
            with get_session() as s:
                s.get(Product, pid).search_query = new_query or None
            with st.spinner("Recherche CJ / AliExpress…"):
                res = matching.match_products(cfg, [pid])
            rescore_all(cfg)
            for e in res.errors:
                st.warning(e)
            for i in res.info:
                st.caption(i)
            if not res.errors:
                st.rerun()
        st.caption(f"Statut : {prod['supplier_status']}"
                   + (f" · dernière recherche {prod['supplier_checked_at']:%d/%m/%Y}" if prod["supplier_checked_at"] else ""))
        if offers:
            odf = pd.DataFrame([{
                "retenue": "●" if o["id"] == prod["selected_offer_id"] else "", "source": o["supplier"],
                "titre": o["title"], "achat €": o["price_eur"], "port €": o["shipping_eur"],
                "coût rendu €": o["landed"],
                "délai": f"{o['delivery_days_min'] or '?'}-{o['delivery_days_max'] or '?'} j",
                "transport": o["shipping_method"], "stock": o["stock"], "note": o["rating"], "ventes": o["orders"],
                "correspondance": o["match_score"], "problèmes": o["problèmes"], "lien": o["url"]} for o in offers])
            st.dataframe(odf, hide_index=True, width="stretch", column_config={
                "achat €": st.column_config.NumberColumn(format="%.2f €"),
                "port €": st.column_config.NumberColumn(format="%.2f €"),
                "coût rendu €": st.column_config.NumberColumn(format="%.2f €"),
                "correspondance": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f"),
                "lien": st.column_config.LinkColumn(display_text="ouvrir")})
            st.caption("Note AliExpress = taux d'avis positifs ramené sur 5. Stock AliExpress non publié par l'API. "
                       "CJ ne publie pas de note vendeur (non exigée).")
            offer_ids = [o["id"] for o in offers]
            idx = 1 + offer_ids.index(prod["selected_offer_id"]) \
                if prod["offer_locked"] and prod["selected_offer_id"] in offer_ids else 0
            s1, s2 = st.columns([3, 1])
            choice = s1.selectbox(
                "Offre à retenir", [None] + offer_ids, index=idx,
                format_func=lambda i: "Automatique (la moins chère conforme)" if i is None else next(
                    f"{o['supplier']} · {o['title'][:70]} · {o['landed'] or '?'} €" for o in offers if o["id"] == i))
            s2.write("")
            if s2.button("Appliquer", width="stretch"):
                with get_session() as s:
                    pp = s.get(Product, pid)
                    pp.offer_locked = choice is not None
                    pp.selected_offer_id = choice
                rescore_all(cfg)
                st.rerun()

        with st.expander("Ajouter / compléter une offre à la main", icon=":material/edit_note:"):
            st.caption("Pour une offre trouvée vous-même, ou un prix issu d'un CSV de pubs sans port/délai. "
                       "Saisissez uniquement des données vérifiées.")
            target = st.selectbox("Offre", ["Nouvelle offre"] + [o["id"] for o in offers],
                                  format_func=lambda i: i if isinstance(i, str) else next(
                                      f"{o['supplier']} · {o['title'][:60]}" for o in offers if o["id"] == i))
            base = next((o for o in offers if o["id"] == target), {}) if not isinstance(target, str) else {}
            with st.form("offer_form"):
                f1, f2, f3 = st.columns(3)
                o_title = f1.text_input("Titre fournisseur", base.get("title") or prod["title"])
                o_url = f1.text_input("URL fournisseur", base.get("url") or "")
                o_price = f2.number_input("Prix d'achat (€)", 0.0, 1000.0, float(base.get("price_eur") or 0), 0.01)
                o_ship = f2.number_input("Livraison France (€)", 0.0, 500.0, float(base.get("shipping_eur") or 0), 0.01)
                o_dmin = f3.number_input("Délai min (j)", 0, 90, int(base.get("delivery_days_min") or 0))
                o_dmax = f3.number_input("Délai max (j)", 0, 90, int(base.get("delivery_days_max") or 0))
                g1, g2 = st.columns(2)
                o_rating = g1.number_input("Note vendeur /5 (0 = inconnue)", 0.0, 5.0, float(base.get("rating") or 0), 0.1)
                o_stock = g2.number_input("Stock (-1 = inconnu)", -1, 10_000_000,
                                          int(base["stock"]) if base.get("stock") is not None else -1)
                if st.form_submit_button("Enregistrer l'offre"):
                    if not (o_price and o_dmax):
                        st.error("Prix d'achat et délai max sont obligatoires.")
                    else:
                        with get_session() as s:
                            upsert_offer(s, pid, {
                                "supplier": base.get("supplier") or "manuel",
                                "supplier_product_id": base.get("supplier_product_id")
                                or f"manuel-{int(datetime.now().timestamp())}",
                                "title": o_title, "url": o_url or None, "price_eur": o_price, "shipping_eur": o_ship,
                                "delivery_days_min": o_dmin or None, "delivery_days_max": o_dmax,
                                "rating": o_rating or None, "stock": None if o_stock < 0 else o_stock,
                                "match_score": base.get("match_score") if base else 1.0})
                            s.get(Product, pid).supplier_status = SUPPLIER_FOUND
                        rescore_all(cfg)
                        st.rerun()

    # ---- Concurrents ----
    with tab_comp:
        if competitors:
            st.dataframe(pd.DataFrame(competitors)[["store", "price", "title", "url"]], hide_index=True,
                         width="stretch", column_config={
                             "store": "boutique", "title": "titre",
                             "url": st.column_config.LinkColumn("lien", display_text="ouvrir"),
                             "price": st.column_config.NumberColumn("prix", format="%.2f €")})
        else:
            st.caption("Aucun concurrent suivi ne vend ce produit.")
        st.markdown("**Publicités**")
        if prod["ad_first_seen"]:
            last = f" · dernière : {prod['ad_last_seen']:%d/%m/%Y}" if prod["ad_last_seen"] else ""
            st.write(f"{prod['advertisers_count'] or '?'} annonceur(s) · première pub vue : "
                     f"{prod['ad_first_seen']:%d/%m/%Y}{last}")
        for link in ad_links[:15]:
            st.markdown(f"- [{link[:90]}]({link})")
        q = (prod["keyword"] or prod["title"]).replace(" ", "%20")
        st.markdown(f"[Meta Ad Library (FR)](https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
                    f"&country=FR&q={q}&search_type=keyword_unordered) · "
                    f"[TikTok Creative Center](https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/fr?keyword={q})")

    # ---- Demande & score ----
    with tab_dem:
        d1, d2 = st.columns(2)
        with d1:
            st.markdown("**Score détaillé**")
            crit = {k: v for k, v in details.items() if not k.startswith("_")}
            if crit:
                total_w = max(sum(v["poids"] for v in crit.values()), 1)
                sd = pd.DataFrame([{"critère": CRITERIA_LABELS.get(k, k), "points": v["points"],
                                    "max": v["poids"] * 100 / total_w, "valeur": v.get("valeur", ""),
                                    "manquant": v.get("manquant", False)} for k, v in crit.items()])
                fig = go.Figure()
                fig.add_bar(y=sd["critère"], x=sd["max"], orientation="h", marker_color="#E2E8F0", hoverinfo="skip")
                fig.add_bar(y=sd["critère"], x=sd["points"], orientation="h", text=sd["valeur"], textposition="auto",
                            marker_color=["#C08A2E" if m else "#0E7C66" for m in sd["manquant"]])
                fig.update_layout(barmode="overlay", height=280, margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
                                  yaxis=dict(autorange="reversed"))
                st.plotly_chart(fig, width="stretch")
                st.caption("En ambre : donnée manquante.")
        with d2:
            st.markdown(f"**Google Trends — « {prod['keyword'] or '?'} »**")
            if trend:
                tdf = pd.DataFrame({"date": pd.to_datetime(list(trend.keys())), "intérêt": list(trend.values())})
                fig = px.line(tdf, x="date", y="intérêt", height=260, color_discrete_sequence=["#0E7C66"])
                fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis_range=[0, 100])
                st.plotly_chart(fig, width="stretch")
            else:
                st.caption("Tendance non mesurée.")
            if prod["keyword"] and st.button("Mesurer la tendance", icon=":material/refresh:"):
                with st.spinner("Google Trends…"):
                    try:
                        data = google_trends.fetch_trend(google_trends._build_client(), prod["keyword"],
                                                         cfg["google_trends"]["geos"], cfg["google_trends"]["timeframe"])
                        with get_session() as s:
                            pp = s.get(Product, pid)
                            pp.trend_mean = data["mean"] if data else 0.0
                            pp.trend_slope = data["slope"] if data else 0.0
                            pp.trend_data = json.dumps(data["series"]) if data else "{}"
                            pp.trend_updated_at = datetime.utcnow()
                        rescore_all(cfg)
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Google Trends indisponible : {exc}")

    # ---- Fiche IA ----
    with tab_ai:
        ai = analyzer.latest_analysis(pid)
        a1, a2 = st.columns([1, 3])
        if a1.button("Régénérer la fiche" if ai else "Générer la fiche avec Claude", icon=":material/auto_awesome:",
                     disabled=not analyzer.has_api_key() or not offer):
            with st.spinner("Claude rédige la fiche…"):
                try:
                    analyzer.generate_listing(pid, cfg, force=bool(ai))
                    rescore_all(cfg)
                    st.rerun()
                except analyzer.AnalyzerError as exc:
                    st.error(str(exc))
        if not offer:
            a2.caption("Disponible une fois un fournisseur retenu.")
        elif not analyzer.has_api_key():
            a2.caption("Ajoutez la clé Anthropic dans Réglages → Clés API.")
        if ai:
            color = {"go": "green", "no-go": "red"}.get(ai.get("verdict"), "amber")
            st.markdown(f"{badge(ai.get('verdict', '?').upper(), color)} wow {ai.get('wow_score')}/10 — "
                        f"{ai.get('justification', '')}", unsafe_allow_html=True)
            v1, v2 = st.columns([3, 2])
            with v1:
                st.markdown(f"### {ai.get('titre_fr')}")
                with st.container(border=True):
                    st.markdown(ai.get("description_html", ""), unsafe_allow_html=True)
                with st.expander("HTML de la description", icon=":material/code:"):
                    st.code(ai.get("description_html", ""), language="html")
            with v2:
                st.markdown("**Accroches publicitaires**")
                for h in ai.get("accroches", []):
                    st.markdown(f"- « {h} »")
                st.markdown(f"**Public cible** — {ai.get('public_cible')}")
                st.markdown(f"**Angle** — {ai.get('angle_marketing')}")
                st.markdown(f"**Tags** — {', '.join(ai.get('tags', []))}")
                st.markdown("**Risques**")
                for k, v in (ai.get("risques") or {}).items():
                    st.markdown(f"- *{k}* : {v}")
            st.caption(f"Générée le {ai['_date']:%d/%m/%Y %H:%M} · {ai.get('_model', '?')}")


# =====================================================================
# Page — Mes tests
# =====================================================================
def page_tests() -> None:
    page_header("Mes tests", "Suivi de la performance des produits en test")
    with get_session() as s:
        recs = s.scalars(select(TestRecord).order_by(TestRecord.updated_at.desc())).all()
        rows = [{"id": r.id, "produit": r.product.title, "product_id": r.product_id, "statut": r.status,
                 "budget dépensé €": r.budget_spent, "CA €": r.revenue, "ROAS": r.roas, "notes": r.notes or "",
                 "maj": r.updated_at} for r in recs]
    if not rows:
        st.info("Aucun produit en test. Ajoutez-en depuis une fiche produit.")
        return
    df = pd.DataFrame(rows)
    spent, rev = df["budget dépensé €"].sum(), df["CA €"].sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Budget total", f"{spent:,.2f} €", border=True)
    m2.metric("CA total", f"{rev:,.2f} €", border=True)
    m3.metric("ROAS global", f"{rev / spent:.2f}" if spent else "—", border=True)
    m4.metric("Validés", int((df["statut"] == "validé").sum()), border=True)

    edited = st.data_editor(
        df, hide_index=True, width="stretch", num_rows="fixed",
        disabled=["id", "produit", "product_id", "ROAS", "maj"],
        column_order=["produit", "statut", "budget dépensé €", "CA €", "ROAS", "notes", "maj"],
        column_config={
            "statut": st.column_config.SelectboxColumn(options=TEST_STATUSES, required=True),
            "budget dépensé €": st.column_config.NumberColumn(min_value=0.0, format="%.2f €"),
            "CA €": st.column_config.NumberColumn(min_value=0.0, format="%.2f €"),
            "ROAS": st.column_config.NumberColumn(format="%.2f", help="CA / budget publicitaire"),
            "notes": st.column_config.TextColumn(width="large"),
        },
        key="tests_editor",
    )
    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("Enregistrer", type="primary", icon=":material/save:"):
        with get_session() as s:
            for _, r in edited.iterrows():
                rec = s.get(TestRecord, int(r["id"]))
                rec.status = r["statut"]
                rec.budget_spent = float(r["budget dépensé €"] or 0)
                rec.revenue = float(r["CA €"] or 0)
                rec.notes = r["notes"]
        st.success("Enregistré")
        st.rerun()
    c2.download_button("Export CSV", df.to_csv(index=False, sep=";").encode("utf-8-sig"),
                       file_name=f"mes-tests_{datetime.now():%Y%m%d}.csv", mime="text/csv", icon=":material/download:")
    with c3:
        to_del = st.selectbox("Retirer un test", [None] + df["id"].tolist(),
                              format_func=lambda i: "—" if i is None else df.set_index("id").loc[i, "produit"][:60])
        if to_del and st.button("Retirer de mes tests", icon=":material/delete:"):
            with get_session() as s:
                s.delete(s.get(TestRecord, int(to_del)))
            st.rerun()
    open_id = st.selectbox("Ouvrir la fiche de…", df["product_id"].tolist(),
                           format_func=lambda i: df.set_index("product_id").loc[i, "produit"][:80])
    st.button("Ouvrir la fiche", icon=":material/open_in_new:", on_click=go_to_product, args=(int(open_id),))


# =====================================================================
# Page — Réglages
# =====================================================================
def _list_input(label: str, values: list, help: str | None = None) -> list[str]:
    raw = st.text_area(label, ", ".join(map(str, values)), help=help)
    return [v.strip() for v in raw.replace("\n", ",").split(",") if v.strip()]


def page_settings() -> None:
    page_header("Réglages", "Économie, seuils, pondérations, sources et clés d'API")
    cfg = load_config()
    t_eco, t_w, t_src, t_keys, t_kw, t_yaml = st.tabs(
        ["Économie & filtres", "Pondérations", "Sources & concurrents", "Clés API", "Mots-clés exclus", "YAML brut"])

    with t_eco:
        e, f = cfg["economics"], cfg["filters"]
        st.markdown("**Coûts par commande**")
        c1, c2, c3, c4 = st.columns(4)
        e["payment_fees_pct"] = c1.number_input("Frais Shopify / paiement (%)", 0.0, 20.0, float(e["payment_fees_pct"]), 0.5)
        e["ad_cost_pct"] = c2.number_input("Coût pub estimé (% du prix)", 0.0, 80.0, float(e["ad_cost_pct"]), 1.0)
        e["target_net_margin_pct"] = c3.number_input("Marge nette visée (%)", 0.0, 60.0,
                                                     float(e["target_net_margin_pct"]), 1.0)
        e["usd_to_eur"] = c4.number_input("Taux USD → EUR", 0.5, 1.5, float(e["usd_to_eur"]), 0.01)
        st.markdown("**Filtres éliminatoires**")
        c1, c2, c3, c4 = st.columns(4)
        f["min_price_eur"] = c1.number_input("Prix conseillé min (€)", 0.0, 1000.0, float(f["min_price_eur"]), 1.0)
        f["max_price_eur"] = c1.number_input("Prix conseillé max (€)", 0.0, 5000.0, float(f["max_price_eur"]), 1.0)
        f["min_net_profit_eur"] = c2.number_input("Profit net min (€)", 0.0, 200.0, float(f["min_net_profit_eur"]), 0.5)
        f["max_delivery_days"] = c2.number_input("Livraison France max (j)", 1, 60, int(f["max_delivery_days"]))
        f["min_supplier_rating"] = c3.number_input("Note vendeur min (/5)", 0.0, 5.0, float(f["min_supplier_rating"]), 0.1)
        f["require_stock"] = c3.toggle("Exiger du stock (si connu)", f["require_stock"])
        f["max_weight_grams"] = c4.number_input("Poids max (g)", 0, 50000, int(f["max_weight_grams"]), 100)
        cfg["matching"]["min_match_score"] = c4.slider("Correspondance min fournisseur", 0.1, 1.0,
                                                       float(cfg["matching"]["min_match_score"]), 0.05)

    with t_w:
        w, sc = cfg["weights"], cfg["scoring"]
        cols = st.columns(len(CRITERIA_LABELS))
        for col, (k, label) in zip(cols, CRITERIA_LABELS.items()):
            w[k] = col.slider(label, 0, 60, int(w.get(k, 0)))
        total = sum(w.get(k, 0) for k in CRITERIA_LABELS)
        (st.success if total == 100 else st.warning)(f"Somme des poids : {total} (le score est ramené sur 100)")
        c1, c2, c3 = st.columns(3)
        sc["missing_data_value"] = c1.slider("Note si donnée manquante", 0.0, 1.0, float(sc["missing_data_value"]), 0.05)
        sc["margin_pct_min"] = c1.number_input("Marge nette % → note 0", 0.0, 50.0, float(sc["margin_pct_min"]), 1.0)
        sc["margin_pct_max"] = c1.number_input("Marge nette % → note max", 1.0, 80.0, float(sc["margin_pct_max"]), 1.0)
        sc["advertisers_ideal_min"] = c2.number_input("Annonceurs idéal min", 1, 100, int(sc["advertisers_ideal_min"]))
        sc["advertisers_ideal_max"] = c2.number_input("Annonceurs idéal max", 1, 500, int(sc["advertisers_ideal_max"]))
        sc["ad_age_target_days"] = c2.number_input("Ancienneté pubs cible (j)", 1, 365, int(sc["ad_age_target_days"]))
        sc["saturation_max_stores"] = c3.number_input("Saturation : note 0 à N concurrents", 1, 100,
                                                      int(sc["saturation_max_stores"]))
        sc["delivery_days_best"] = c3.number_input("Délai idéal (j)", 1, 30, int(sc["delivery_days_best"]))
        sc["cluster_similarity"] = c3.slider("Regroupement des concurrents (similarité)", 0.3, 0.9,
                                             float(sc["cluster_similarity"]), 0.05)

    with t_src:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Boutiques Shopify concurrentes**")
            cfg["shopify"]["enabled"] = st.toggle("Activer", cfg["shopify"]["enabled"])
            cfg["shopify"]["stores"] = _list_input("Domaines (séparés par des virgules ou un par ligne)",
                                                   cfg["shopify"]["stores"], "ex. maboutique.fr, concurrent.com")
            cfg["shopify"]["max_pages"] = st.number_input("Pages max par boutique (250 produits/page)", 1, 20,
                                                          int(cfg["shopify"]["max_pages"]))
            cfg["shopify"]["best_sellers_only"] = st.toggle("Ne garder que les best-sellers (si classement lisible)",
                                                            cfg["shopify"].get("best_sellers_only", False))
            st.markdown("**Google Trends**")
            g = cfg["google_trends"]
            g["enabled"] = st.toggle("Activer Google Trends", g["enabled"])
            g["geos"] = _list_input("Zones (codes pays)", g["geos"])
            g["max_keywords_per_run"] = st.number_input("Mots-clés max par collecte", 1, 200, int(g["max_keywords_per_run"]))
        with c2:
            st.markdown("**Matching fournisseur**")
            m = cfg["matching"]
            cfg["cj"]["enabled"] = st.toggle("CJ Dropshipping", cfg["cj"]["enabled"])
            cfg["aliexpress"]["enabled"] = st.toggle("AliExpress (API affiliés)", cfg["aliexpress"]["enabled"])
            m["max_products_per_run"] = st.number_input("Produits recherchés par collecte", 1, 500,
                                                        int(m["max_products_per_run"]))
            m["details_per_supplier"] = st.number_input("Candidats détaillés par source (prix + port)", 1, 10,
                                                        int(m.get("details_per_supplier", 3)))
            m["recheck_days"] = st.number_input("Re-chercher après (jours)", 1, 90, int(m["recheck_days"]))
            m["translate_queries"] = st.toggle("Traduire les titres en requêtes anglaises (Claude)", m["translate_queries"])
            st.markdown("**Génération IA & export Shopify**")
            a = cfg["analyzer"]
            a["model"] = st.text_input("Modèle Claude", a["model"])
            a["effort"] = st.selectbox("Effort", ["low", "medium", "high"], ["low", "medium", "high"].index(a["effort"]))
            a["top_n"] = st.number_input("Top N", 1, 100, int(a["top_n"]))
            ex = cfg["shopify_export"]
            ex["vendor"] = st.text_input("Vendor (export Shopify)", ex.get("vendor", ""))
            ex["status"] = st.selectbox("Statut à l'import", ["draft", "active"],
                                        ["draft", "active"].index(ex.get("status", "draft")))

    with t_keys:
        st.caption("Stockées dans le fichier .env du projet (exclu de git). Laissez un champ vide pour ne pas le modifier.")
        with st.form("keys"):
            new_vals = {}
            for name, label in API_KEYS.items():
                cur = masked_key(name)
                new_vals[name] = st.text_input(label, type="password",
                                               placeholder=f"actuelle : {cur}" if cur else "non définie")
            if st.form_submit_button("Enregistrer les clés", icon=":material/key:"):
                changed = [n for n, v in new_vals.items() if v.strip()]
                for n in changed:
                    set_api_key(n, new_vals[n])
                st.success(f"{len(changed)} clé(s) enregistrée(s)" if changed else "Aucune modification")
        st.markdown("Où obtenir les clés : [CJ Dropshipping](https://cjdropshipping.com) (Mon CJ → API) · "
                    "[AliExpress Open Platform](https://openservice.aliexpress.com) (programme affiliés) · "
                    "[Anthropic](https://console.anthropic.com)")

    with t_kw:
        st.caption("Un produit contenant un de ces mots (titre, catégorie, tags, description) est exclu.")
        kw = cfg["filters"]["excluded_keywords"]
        for motif in list(kw):
            kw[motif] = _list_input(motif.replace("_", " ").capitalize(), kw[motif])

    with t_yaml:
        raw = st.text_area("config.yaml", yaml.safe_dump(load_config(), allow_unicode=True, sort_keys=False), height=500)
        if st.button("Enregistrer le YAML"):
            try:
                save_config(yaml.safe_load(raw))
                rescore_all(load_config())
                st.success("Configuration enregistrée")
            except yaml.YAMLError as exc:
                st.error(f"YAML invalide : {exc}")

    st.divider()
    if st.button("Enregistrer et recalculer", type="primary", icon=":material/save:"):
        save_config(cfg)
        with st.spinner("Recalcul…"):
            res = rescore_all(cfg)
        st.success(f"Réglages enregistrés · {res['eligible']} produit(s) prêt(s) sur {res['groups']}")


# =====================================================================
# Navigation
# =====================================================================
if st.session_state.get("page") not in PAGES:
    st.session_state["page"] = PAGES[0]
with st.sidebar:
    st.markdown('<div class="ph-brand">PRODUCT<span>HUNTER</span></div>'
                '<div class="ph-tag">Sourcing e-commerce · FR</div>', unsafe_allow_html=True)
    st.write("")
    st.radio("Navigation", PAGES, key="page", label_visibility="collapsed",
             format_func=lambda p: f"{PAGE_ICONS[PAGES.index(p)]} {p}")
    st.divider()

{PAGES[0]: page_products, PAGES[1]: page_product, PAGES[2]: page_tests, PAGES[3]: page_settings}[st.session_state["page"]]()

"""Interface Streamlit de product-hunter.

Lancement :  streamlit run app.py
Pages : Découverte · Fiche produit · Mes tests · Réglages
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

import analyzer
from collectors import google_trends
from db import TEST_STATUSES, Product, Run, TestRecord, get_session
from pipeline import run_collection
from scoring import CRITERIA_LABELS, estimated_cost, rescore_all
from settings import load_config, save_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
st.set_page_config(page_title="Product Hunter", page_icon=":material/insights:", layout="wide")

PAGES = ["Découverte", "Fiche produit", "Mes tests", "Réglages"]
PAGE_ICONS = [":material/travel_explore:", ":material/inventory_2:", ":material/monitoring:", ":material/tune:"]

# Ajustements visuels complémentaires au thème (.streamlit/config.toml)
st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; max-width: 1400px;}
  h1 {letter-spacing: -0.02em;}
  .ph-header {border-bottom: 1px solid #E2E8F0; padding-bottom: .8rem; margin-bottom: 1.2rem;}
  .ph-header h1 {font-size: 1.75rem; margin: 0; padding: 0;}
  .ph-header p {color: #64748B; margin: .25rem 0 0 0; font-size: .95rem;}
  .ph-brand {font-weight: 700; font-size: 1.15rem; letter-spacing: .02em; color: #F8FAFC;}
  .ph-brand span {color: #34D399;}
  .ph-tag {color: #94A3B8; font-size: .78rem; text-transform: uppercase; letter-spacing: .08em;}
  [data-testid="stMetricValue"] {font-weight: 600;}
</style>
""", unsafe_allow_html=True)


def page_header(title: str, subtitle: str) -> None:
    st.markdown(f'<div class="ph-header"><h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


# =====================================================================
# Helpers données
# =====================================================================
def load_products_df() -> pd.DataFrame:
    cfg = load_config()
    rows = []
    with get_session() as s:
        for p in s.scalars(select(Product)):
            cost, est = estimated_cost(p, cfg)
            rows.append({
                "id": p.id,
                "image": p.image_url or "",
                "titre": p.title,
                "score": p.score,
                "prix €": p.price_eur,
                "achat €": cost,
                "achat estimé": est,
                "marge x": round(p.price_eur / cost, 2) if p.price_eur and cost else None,
                "marge €": round(p.price_eur - cost, 2) if p.price_eur and cost else None,
                "annonceurs": p.advertisers_count,
                "1ère pub": p.ad_first_seen.date() if p.ad_first_seen else None,
                "tendance": p.trend_mean,
                "pente": p.trend_slope,
                "boutiques": p.saturation_stores,
                "wow": p.wow_score,
                "source": p.source,
                "boutique": p.store,
                "catégorie": p.category or "—",
                "mot-clé": p.keyword,
                "lien": p.url,
                "fournisseur": p.supplier_url,
                "exclu": p.excluded,
                "raisons exclusion": ", ".join(p.json_field("exclusion_reasons", [])),
                "maj": p.updated_at,
            })
    return pd.DataFrame(rows)


def go_to_product(pid: int) -> None:
    st.session_state["product_id"] = pid
    st.session_state["page"] = PAGES[1]


def last_run_report() -> tuple[datetime, dict] | None:
    with get_session() as s:
        run = s.scalar(select(Run).order_by(Run.started_at.desc()))
        return (run.started_at, json.loads(run.report_json)) if run else None


def show_report(report: dict) -> None:
    for src, rep in report.items():
        if src == "scoring":
            st.write(f"**Score** : {rep}")
            continue
        icon = ":green[●]" if not rep.get("errors") else (":orange[●]" if rep.get("items") else ":red[●]")
        st.write(f"{icon} **{src}** — {rep.get('items', 0)} élément(s)")
        for e in rep.get("errors", []):
            st.caption(f":red[{e}]")
        for i in rep.get("info", [])[:8]:
            st.caption(f"· {i}")


# =====================================================================
# Page 1 — Découverte
# =====================================================================
def page_discovery() -> None:
    page_header("Découverte", "Produits détectés, filtrés et classés par potentiel")
    cfg = load_config()

    with st.expander("Lancer une collecte", expanded=False, icon=":material/sync:"):
        c1, c2 = st.columns([2, 3])
        with c1:
            src_shopify = st.checkbox("Boutiques Shopify", value=cfg["shopify"].get("enabled", True))
            src_amazon = st.checkbox("Amazon Movers & Shakers", value=cfg["amazon"].get("enabled", True))
            src_ads = st.checkbox("Import CSV pubs (Minea / PPSPY / Meta)", value=True)
            src_trends = st.checkbox("Google Trends (lent)", value=cfg["google_trends"].get("enabled", True))
        with c2:
            uploads = st.file_uploader("CSV exportés (Minea, PPSPY, Meta Ad Library)", type=["csv"],
                                       accept_multiple_files=True)
        if st.button("Lancer la collecte", type="primary"):
            sources = tuple(n for n, on in [("shopify", src_shopify), ("amazon", src_amazon),
                                             ("ads", src_ads), ("trends", src_trends)] if on)
            bar = st.progress(0.0, text="Démarrage…")
            with st.status("Collecte en cours…", expanded=True) as status:
                report = run_collection(
                    cfg, sources=sources,
                    uploaded_csvs=[(f.name, f.getvalue()) for f in uploads or []],
                    progress=lambda msg, pct: bar.progress(min(pct, 1.0), text=msg),
                )
                show_report(report)
                status.update(label="Collecte terminée", state="complete")

    run = last_run_report()
    if run:
        with st.expander(f"Dernière collecte : {run[0]:%d/%m/%Y %H:%M} UTC"):
            show_report(run[1])

    df = load_products_df()
    if df.empty:
        st.info("Aucun produit en base. Lancez une collecte (et/ou importez un CSV de pubs).")
        return

    # ---- Filtres ----
    with st.sidebar:
        st.subheader("Filtres")
        show_excluded = st.toggle("Afficher les produits exclus", value=False)
        search = st.text_input("Recherche dans le titre")
        prices = df["prix €"].dropna()
        pmax = float(max(prices.max() if not prices.empty else 100, 1))
        price_range = st.slider("Prix de vente (€)", 0.0, pmax,
                                (0.0, pmax) if show_excluded else (float(cfg["filters"]["min_price_eur"]),
                                                                   min(float(cfg["filters"]["max_price_eur"]), pmax)))
        min_markup = st.slider("Marge minimum (x)", 0.0, 10.0, 0.0, 0.5)
        min_score = st.slider("Score minimum", 0, 100, 0)
        cats = st.multiselect("Catégorie", sorted(df["catégorie"].unique()))
        srcs = st.multiselect("Source", sorted(df["source"].unique()))

    view = df if show_excluded else df[~df["exclu"]]
    view = view[(view["score"].fillna(0) >= min_score)]
    view = view[view["prix €"].isna() | view["prix €"].between(*price_range)]
    if min_markup:
        view = view[view["marge x"].fillna(0) >= min_markup]
    if cats:
        view = view[view["catégorie"].isin(cats)]
    if srcs:
        view = view[view["source"].isin(srcs)]
    if search:
        view = view[view["titre"].str.contains(search, case=False, na=False)]
    view = view.sort_values("score", ascending=False, na_position="last")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Produits en base", len(df), border=True)
    m2.metric("Éligibles", int((~df["exclu"]).sum()), border=True)
    m3.metric("Affichés", len(view), border=True)
    m4.metric("Meilleur score", f"{view['score'].max():.0f}/100" if not view.empty else "—", border=True)

    # ---- Analyse IA du top ----
    c1, c2, c3 = st.columns([2, 1, 2])
    top_n = c2.number_input("Top N", 1, 50, int(cfg["analyzer"].get("top_n", 10)), label_visibility="collapsed")
    if c1.button(f"Analyser le top {top_n} avec Claude", icon=":material/psychology:", disabled=not analyzer.has_api_key(),
                 help=None if analyzer.has_api_key() else "Ajoutez ANTHROPIC_API_KEY dans .env"):
        bar = st.progress(0.0)
        with st.spinner("Analyse IA en cours (résultats mis en cache)…"):
            res = analyzer.analyze_top(cfg, top_n, progress=lambda m, p: bar.progress(p, text=m))
        bar.empty()
        st.success(f"{res['analysés']} produit(s) analysé(s) dont {res['depuis le cache']} depuis le cache.")
        for e in res["erreurs"]:
            st.warning(e)
    if not analyzer.has_api_key():
        c3.caption(":material/info: Analyse IA désactivée : ANTHROPIC_API_KEY absente du fichier .env")

    # ---- Tableau ----
    cols = ["image", "titre", "score", "prix €", "marge x", "marge €", "annonceurs", "1ère pub", "tendance",
            "boutiques", "wow", "source", "boutique", "catégorie", "lien"]
    if show_excluded:
        cols.append("raisons exclusion")
    event = st.dataframe(
        view[cols], hide_index=True, use_container_width=True, height=520,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "image": st.column_config.ImageColumn("", width="small"),
            "titre": st.column_config.TextColumn(width="large"),
            "score": st.column_config.ProgressColumn("score", min_value=0, max_value=100, format="%.0f"),
            "prix €": st.column_config.NumberColumn(format="%.2f €"),
            "marge €": st.column_config.NumberColumn(format="%.2f €"),
            "marge x": st.column_config.NumberColumn(format="x%.1f"),
            "tendance": st.column_config.NumberColumn(format="%.0f"),
            "wow": st.column_config.NumberColumn(format="%.0f/10"),
            "lien": st.column_config.LinkColumn(display_text="ouvrir"),
        },
    )
    rows = event.selection.rows if event and event.selection else []
    c1, c2 = st.columns([1, 1])
    if rows:
        pid = int(view.iloc[rows[0]]["id"])
        c1.button("Ouvrir la fiche produit", icon=":material/open_in_new:", type="primary", on_click=go_to_product, args=(pid,))
    else:
        c1.caption("Sélectionnez une ligne pour ouvrir sa fiche.")
    c2.download_button("Export CSV", icon=":material/download:", data=view.drop(columns=["image"]).to_csv(index=False, sep=";").encode("utf-8-sig"),
                       file_name=f"product-hunter_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv")


# =====================================================================
# Page 2 — Fiche produit
# =====================================================================
def page_product() -> None:
    page_header("Fiche produit", "Analyse détaillée d'une opportunité")
    cfg = load_config()
    df = load_products_df()
    if df.empty:
        st.info("Aucun produit en base.")
        return
    df = df.sort_values("score", ascending=False, na_position="last")
    ids = df["id"].tolist()
    labels = {r.id: f"{r.score or 0:.0f} · {r.titre[:90]} ({r.source})" for r in df.itertuples()}
    current = st.session_state.get("product_id")
    pid = st.selectbox("Produit", ids, index=ids.index(current) if current in ids else 0,
                       format_func=lambda i: labels[i])
    st.session_state["product_id"] = pid

    with get_session() as s:
        p = s.get(Product, pid)
        details = p.json_field("score_details", {})
        trend = p.json_field("trend_data", {})
        ad_links = p.json_field("ad_links", [])
        reasons = p.json_field("exclusion_reasons", [])
        cost, est = estimated_cost(p, cfg)
        prod = {c.name: getattr(p, c.name) for c in Product.__table__.columns}

    # ---- En-tête ----
    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        if prod["image_url"]:
            st.image(prod["image_url"], use_container_width=True)
    with c2:
        st.subheader(prod["title"])
        st.write(f"**Source :** {prod['source']} · **Boutique :** {prod['store'] or '—'} · "
                 f"**Catégorie :** {prod['category'] or '—'}")
        st.write(f"**Prix de vente :** {prod['price_eur'] or '—'} € · **Prix d'achat :** "
                 f"{cost or '—'} €{' (estimé)' if est and cost else ''}")
        if prod["url"]:
            st.link_button("Voir le produit", prod["url"], icon=":material/storefront:")
        if prod["supplier_url"]:
            st.link_button("Fournisseur", icon=":material/local_shipping:", url=prod["supplier_url"])
        else:
            q = (prod["keyword"] or prod["title"]).replace(" ", "+")
            st.link_button("Chercher sur AliExpress", icon=":material/search:", url=f"https://fr.aliexpress.com/w/wholesale-{q}.html")
        if reasons:
            st.error("Exclu : " + " · ".join(reasons))
        if prod["description"]:
            with st.expander("Description"):
                st.write(prod["description"])
    with c3:
        st.metric("Score", f"{prod['score'] or 0:.0f}/100", border=True)
        st.metric("Annonceurs", prod["advertisers_count"] or "—", border=True)
        st.metric("Boutiques concurrentes", prod["saturation_stores"] or "—", border=True)
        if st.button("Ajouter à mes tests", icon=":material/add:", use_container_width=True):
            with get_session() as s:
                exists = s.scalar(select(TestRecord).where(TestRecord.product_id == pid))
                if not exists:
                    s.add(TestRecord(product_id=pid, status="à tester"))
            st.toast("Ajouté à « Mes tests »" if not exists else "Déjà dans « Mes tests »")

    # ---- Édition ----
    with st.expander("Corriger les données (prix d'achat, fournisseur, mot-clé Trends)", icon=":material/edit:"):
        with st.form("edit"):
            e1, e2, e3 = st.columns(3)
            new_cost = e1.number_input("Prix d'achat réel (€)", 0.0, 10000.0, float(prod["supplier_cost_eur"] or 0.0), 0.1)
            new_price = e1.number_input("Prix de vente (€)", 0.0, 10000.0, float(prod["price_eur"] or 0.0), 0.1)
            new_supplier = e2.text_input("Lien fournisseur", prod["supplier_url"] or "")
            new_kw = e3.text_input("Mot-clé Google Trends", prod["keyword"] or "")
            if st.form_submit_button("Enregistrer et recalculer"):
                with get_session() as s:
                    p = s.get(Product, pid)
                    p.supplier_cost_eur = new_cost or None
                    p.price_eur = new_price or None
                    p.supplier_url = new_supplier or None
                    if new_kw != (p.keyword or ""):
                        p.keyword, p.trend_updated_at = new_kw or None, None
                rescore_all(cfg)
                st.rerun()

    # ---- Score détaillé + Trends ----
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Score détaillé")
        if details:
            sd = pd.DataFrame([{"critère": CRITERIA_LABELS.get(k, k), "points": v["points"],
                                "max": v["poids"] * 100 / max(sum(d["poids"] for d in details.values()), 1),
                                "valeur": v.get("valeur", ""), "manquant": v.get("manquant", False)}
                               for k, v in details.items()])
            fig = go.Figure()
            fig.add_bar(y=sd["critère"], x=sd["max"], orientation="h", marker_color="#E2E8F0",
                        name="max", hoverinfo="skip")
            fig.add_bar(y=sd["critère"], x=sd["points"], orientation="h", name="points",
                        marker_color=["#C08A2E" if m else "#0E7C66" for m in sd["manquant"]],
                        text=sd["valeur"], textposition="auto")
            fig.update_layout(barmode="overlay", height=300, margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
                              yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig, use_container_width=True)
            st.caption("En ambre : donnée manquante (note par défaut).")
    with c2:
        st.subheader(f"Google Trends — « {prod['keyword'] or '?'} »")
        if trend:
            tdf = pd.DataFrame({"date": pd.to_datetime(list(trend.keys())), "intérêt": list(trend.values())})
            fig = px.line(tdf, x="date", y="intérêt", height=300)
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis_range=[0, 100])
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"Moyenne récente {prod['trend_mean']} · pente {prod['trend_slope']:+.2f} · "
                       f"zones {', '.join(cfg['google_trends']['geos'])}")
        else:
            st.info("Tendance non mesurée (ou aucun volume de recherche).")
        if prod["keyword"] and st.button("Mesurer la tendance", icon=":material/refresh:"):
            with st.spinner("Interrogation de Google Trends…"):
                try:
                    data = google_trends.fetch_trend(google_trends._build_client(), prod["keyword"],
                                                     cfg["google_trends"]["geos"], cfg["google_trends"]["timeframe"])
                    with get_session() as s:
                        p = s.get(Product, pid)
                        p.trend_mean = data["mean"] if data else 0.0
                        p.trend_slope = data["slope"] if data else 0.0
                        p.trend_data = json.dumps(data["series"]) if data else "{}"
                        p.trend_updated_at = datetime.utcnow()
                    rescore_all(cfg)
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Google Trends indisponible : {exc}")

    # ---- Analyse IA ----
    st.subheader("Analyse IA")
    analysis = analyzer.latest_analysis(pid)
    b1, b2 = st.columns([1, 3])
    label = "Ré-analyser" if analysis else "Analyser avec Claude"
    if b1.button(label, disabled=not analyzer.has_api_key()):
        with st.spinner("Claude analyse le produit…"):
            try:
                analyzer.analyze_product(pid, cfg, force=bool(analysis))
                rescore_all(cfg)
                st.rerun()
            except analyzer.AnalyzerError as exc:
                st.error(str(exc))
    if not analyzer.has_api_key():
        b2.caption("Ajoutez ANTHROPIC_API_KEY dans le fichier .env pour activer l'analyse.")
    if analysis:
        verdict = analysis.get("verdict", "?")
        color = {"go": "green", "no-go": "red"}.get(verdict, "orange")
        st.markdown(f"### Verdict : :{color}[{verdict.upper()}]  ·  wow {analysis.get('wow_score')}/10")
        st.write(analysis.get("justification", ""))
        a1, a2 = st.columns(2)
        with a1:
            st.markdown(f"**Angle marketing** — {analysis.get('angle_marketing')}")
            st.markdown(f"**Problème résolu** — {analysis.get('probleme_resolu')}")
            st.markdown(f"**Public cible** — {analysis.get('public_cible')}")
            st.markdown(f"**Prix conseillé** — {analysis.get('prix_conseille_eur')} €")
        with a2:
            st.markdown("**Accroches publicitaires**")
            for h in analysis.get("accroches", []):
                st.markdown(f"- « {h} »")
            st.markdown("**Risques**")
            for k, v in (analysis.get("risques") or {}).items():
                st.markdown(f"- *{k}* : {v}")
        st.caption(f"Analyse du {analysis['_date']:%d/%m/%Y %H:%M} · modèle {analysis.get('_model', '?')}")

    # ---- Pubs ----
    st.subheader("Publicités")
    if prod["ad_first_seen"]:
        last = f" · dernière : {prod['ad_last_seen']:%d/%m/%Y}" if prod["ad_last_seen"] else ""
        st.write(f"Première pub vue : {prod['ad_first_seen']:%d/%m/%Y}{last}")
    elif not ad_links:
        st.caption("Aucune donnée publicitaire : importez un CSV Minea / PPSPY / Meta Ad Library.")
    for link in ad_links:
        st.markdown(f"- [{link[:90]}]({link})")
    q = (prod["keyword"] or prod["title"]).replace(" ", "%20")
    st.markdown(f"[Rechercher dans la Meta Ad Library (FR)](https://www.facebook.com/ads/library/?active_status=all"
                f"&ad_type=all&country=FR&q={q}&search_type=keyword_unordered) · "
                f"[TikTok Creative Center](https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/fr?keyword={q})")


# =====================================================================
# Page 3 — Mes tests
# =====================================================================
def page_tests() -> None:
    page_header("Mes tests", "Suivi de la performance des produits en test")
    with get_session() as s:
        recs = s.scalars(select(TestRecord).order_by(TestRecord.updated_at.desc())).all()
        rows = [{"id": r.id, "produit": r.product.title, "product_id": r.product_id, "statut": r.status,
                 "budget dépensé €": r.budget_spent, "CA €": r.revenue, "ROAS": r.roas, "notes": r.notes or "",
                 "maj": r.updated_at} for r in recs]
    if not rows:
        st.info("Aucun produit en test. Ajoutez-en depuis une fiche produit (bouton « Ajouter à mes tests »).")
        return
    df = pd.DataFrame(rows)

    m1, m2, m3, m4 = st.columns(4)
    spent, rev = df["budget dépensé €"].sum(), df["CA €"].sum()
    m1.metric("Budget total", f"{spent:,.2f} €", border=True)
    m2.metric("CA total", f"{rev:,.2f} €", border=True)
    m3.metric("ROAS global", f"{rev / spent:.2f}" if spent else "—", border=True)
    m4.metric("Validés", int((df["statut"] == "validé").sum()), border=True)

    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, num_rows="fixed",
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
    c2.download_button("Export CSV", icon=":material/download:", data=df.to_csv(index=False, sep=";").encode("utf-8-sig"),
                       file_name=f"mes-tests_{datetime.now():%Y%m%d}.csv", mime="text/csv")
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
# Page 4 — Réglages
# =====================================================================
def _list_input(label: str, values: list, help: str | None = None) -> list[str]:
    raw = st.text_area(label, ", ".join(map(str, values)), help=help)
    return [v.strip() for v in raw.replace("\n", ",").split(",") if v.strip()]


def page_settings() -> None:
    page_header("Réglages", "Seuils, pondérations et sources de données")
    cfg = load_config()
    tab_f, tab_w, tab_s, tab_k, tab_yaml = st.tabs(
        ["Filtres", "Pondérations du score", "Sources", "Mots-clés exclus", "YAML brut"])

    with tab_f:
        f = cfg["filters"]
        c1, c2, c3 = st.columns(3)
        f["min_price_eur"] = c1.number_input("Prix de vente min (€)", 0.0, 1000.0, float(f["min_price_eur"]), 1.0)
        f["max_price_eur"] = c1.number_input("Prix de vente max (€)", 0.0, 5000.0, float(f["max_price_eur"]), 1.0)
        f["min_markup"] = c2.number_input("Coefficient min (vente / achat)", 1.0, 20.0, float(f["min_markup"]), 0.5)
        f["estimated_cost_ratio"] = c2.number_input("Prix d'achat estimé (ratio du prix de vente si inconnu)",
                                                    0.05, 0.9, float(f["estimated_cost_ratio"]), 0.05)
        f["max_weight_grams"] = c3.number_input("Poids max (g)", 0, 50000, int(f["max_weight_grams"]), 100)

    with tab_w:
        w, sc = cfg["weights"], cfg["scoring"]
        cols = st.columns(3)
        for i, (k, label) in enumerate(CRITERIA_LABELS.items()):
            w[k] = cols[i % 3].slider(label, 0, 50, int(w.get(k, 0)))
        total = sum(w.values())
        (st.success if total == 100 else st.warning)(f"Somme des poids : {total} (le score est ramené sur 100)")
        st.divider()
        c1, c2, c3 = st.columns(3)
        sc["missing_data_value"] = c1.slider("Note si donnée manquante", 0.0, 1.0, float(sc["missing_data_value"]), 0.05)
        sc["markup_min"] = c1.number_input("Marge : note 0 à x", 1.0, 10.0, float(sc["markup_min"]), 0.5)
        sc["markup_max"] = c1.number_input("Marge : note max à x", 1.5, 20.0, float(sc["markup_max"]), 0.5)
        sc["advertisers_ideal_min"] = c2.number_input("Annonceurs idéal min", 1, 100, int(sc["advertisers_ideal_min"]))
        sc["advertisers_ideal_max"] = c2.number_input("Annonceurs idéal max", 1, 500, int(sc["advertisers_ideal_max"]))
        sc["advertisers_hard_max"] = c2.number_input("Annonceurs : note 0 au-delà de", 2, 2000, int(sc["advertisers_hard_max"]))
        sc["ad_age_target_days"] = c3.number_input("Ancienneté pubs cible (jours)", 1, 365, int(sc["ad_age_target_days"]))
        sc["saturation_max_stores"] = c3.number_input("Saturation : note 0 à N boutiques", 1, 100, int(sc["saturation_max_stores"]))
        sc["title_similarity"] = c3.slider("Similarité de titres (rapprochement pubs)", 0.4, 1.0, float(sc["title_similarity"]), 0.02)

    with tab_s:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Shopify**")
            cfg["shopify"]["enabled"] = st.toggle("Activer Shopify", cfg["shopify"]["enabled"])
            cfg["shopify"]["stores"] = _list_input("Boutiques concurrentes (domaines)", cfg["shopify"]["stores"],
                                                   "ex. maboutique.fr, concurrent.com")
            cfg["shopify"]["max_pages"] = st.number_input("Pages max (250 produits/page)", 1, 20, int(cfg["shopify"]["max_pages"]))
            st.markdown("**Amazon**")
            cfg["amazon"]["enabled"] = st.toggle("Activer Amazon", cfg["amazon"]["enabled"])
            cfg["amazon"]["categories"] = _list_input("Catégories Movers & Shakers", cfg["amazon"]["categories"],
                                                      "segment d'URL après /gp/movers-and-shakers/")
        with c2:
            st.markdown("**Google Trends**")
            g = cfg["google_trends"]
            g["enabled"] = st.toggle("Activer Google Trends", g["enabled"])
            g["geos"] = _list_input("Zones (codes pays)", g["geos"], "FR, BE, DE, ES, IT…")
            g["timeframe"] = st.selectbox("Période", ["today 1-m", "today 3-m", "today 12-m"],
                                          ["today 1-m", "today 3-m", "today 12-m"].index(g["timeframe"]))
            g["max_keywords_per_run"] = st.number_input("Mots-clés max par collecte", 1, 200, int(g["max_keywords_per_run"]))
            g["delay_s"] = st.number_input("Délai entre requêtes (s)", 1, 120, int(g["delay_s"]))
            st.markdown("**Analyse IA**")
            a = cfg["analyzer"]
            a["model"] = st.text_input("Modèle Claude", a["model"])
            a["effort"] = st.selectbox("Effort", ["low", "medium", "high"], ["low", "medium", "high"].index(a["effort"]))
            a["top_n"] = st.number_input("Top N à analyser", 1, 100, int(a["top_n"]))
            a["cache_days"] = st.number_input("Cache des analyses (jours)", 0, 365, int(a["cache_days"]))

    with tab_k:
        st.caption("Un produit contenant un de ces mots (titre, catégorie, tags, description) est exclu.")
        kw = cfg["filters"]["excluded_keywords"]
        for motif in list(kw):
            kw[motif] = _list_input(motif.replace("_", " ").capitalize(), kw[motif])

    with tab_yaml:
        st.caption("Édition avancée : remplace toute la configuration.")
        raw = st.text_area("config.yaml", yaml.safe_dump(load_config(), allow_unicode=True, sort_keys=False), height=500)
        if st.button("Enregistrer le YAML"):
            try:
                save_config(yaml.safe_load(raw))
                st.success("Configuration enregistrée")
                rescore_all(load_config())
            except yaml.YAMLError as exc:
                st.error(f"YAML invalide : {exc}")

    st.divider()
    if st.button("Enregistrer et recalculer les scores", type="primary", icon=":material/save:"):
        save_config(cfg)
        with st.spinner("Recalcul…"):
            res = rescore_all(cfg)
        st.success(f"Réglages enregistrés · {res['eligible']} produits éligibles sur {res['products']}")


# =====================================================================
# Navigation
# =====================================================================
if "page" not in st.session_state:
    st.session_state["page"] = PAGES[0]
with st.sidebar:
    st.markdown('<div class="ph-brand">PRODUCT<span>HUNTER</span></div>'
                '<div class="ph-tag">Sourcing e-commerce · FR / EU</div>', unsafe_allow_html=True)
    st.write("")
    st.radio("Navigation", PAGES, key="page", label_visibility="collapsed",
             format_func=lambda p: f"{PAGE_ICONS[PAGES.index(p)]} {p}")
    st.divider()

{PAGES[0]: page_discovery, PAGES[1]: page_product, PAGES[2]: page_tests, PAGES[3]: page_settings}[st.session_state["page"]]()

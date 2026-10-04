# product-hunter

Outil de sourcing de produits **dropshipping niche pour Shopify (marché France)**. Il détecte la demande, trouve le **même produit chez un fournisseur** (CJ Dropshipping, AliExpress), calcule la **marge réelle** par commande, élimine ce qui ne passe pas vos critères, et prépare des **fiches Shopify prêtes à importer**.

> **Aucune donnée inventée.** Sans offre fournisseur réelle (prix d'achat, frais de port France, délai), un produit n'a ni marge ni prix conseillé : il est marqué *fournisseur introuvable* (ou *non cherché*) et reste hors du top.

## Flux

1. **Demande** : catalogue et best-sellers des boutiques Shopify concurrentes (`/products.json`), import CSV de pubs (Minea, PPSPY, Meta Ad Library), Google Trends FR.
2. **Regroupement** : un même produit vendu par plusieurs boutiques (ou en plusieurs couleurs) forme un seul groupe ; la liste des concurrents et leurs prix sont conservés.
3. **Matching fournisseur** : le titre (souvent en français) est traduit par Claude en requête anglaise, puis cherché chez **CJ Dropshipping** et **AliExpress**. Pour les meilleurs candidats : prix exact de la variante, **frais de port et délai réels vers la France**, stock, note, ventes, URL. Import CSV fournisseur en complément.
4. **Marge réelle** : prix d'achat + livraison + frais de paiement (3 %) + coût pub estimé (25 % du prix) → profit net par commande et **prix de vente conseillé**.
5. **Filtres éliminatoires** : livraison France ≤ 12 j, note vendeur ≥ 4,5, stock disponible, prix conseillé 30-80 €, profit net minimum, pas de marque / IP, pas de produit réglementé, fragile, à batterie, cosmétique ou ingéré.
6. **Score /100** : marge, demande, saturation, fiabilité fournisseur, effet « wow ».
7. **Génération IA** (Claude) : titre FR, description Shopify HTML, 3 accroches pub, public cible, tags, SEO, risques, verdict go / no-go.
8. **Export Shopify** : CSV au format officiel d'import produits.

## Installation

Prérequis : Python 3.11+.

```bash
cd product-hunter
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Lancement (depuis ce dossier, pour appliquer le thème) :

```bash
.venv/bin/streamlit run app.py
```

## Clés d'API

À saisir dans **Réglages → Clés API** (écrites dans `.env`, exclu de git) :

| Clé | Rôle | Où l'obtenir |
|---|---|---|
| `CJ_API_KEY` | recherche CJ, prix variante, frais de port, stock | compte CJ Dropshipping → API (format `CJUserNum@api@…`) |
| `ALIEXPRESS_APP_KEY` / `ALIEXPRESS_APP_SECRET` | recherche AliExpress + frais de port FR | AliExpress Open Platform, programme affiliés |
| `ALIEXPRESS_TRACKING_ID` | (optionnel) liens affiliés | idem |
| `ANTHROPIC_API_KEY` | traduction des requêtes, fiches IA | console.anthropic.com |

Sans clé fournisseur, l'outil fonctionne quand même avec l'**import CSV fournisseur** ou la **saisie manuelle d'une offre** dans la fiche produit.

## Sources fournisseur : ce que chaque API fournit réellement

| Donnée | CJ Dropshipping | AliExpress (API affiliés) |
|---|---|---|
| Prix d'achat | prix de la variante la moins chère (USD → EUR) | prix en EUR |
| Livraison France | `freightCalculate` CN → FR : méthode la moins chère respectant le délai max | `product.shipping.get` : frais + délai min/max |
| Stock | stock entrepôt | **non publié** (inconnu) |
| Note | **non publiée** (CJ est l'entrepôt : note non exigée) | taux d'avis positifs ramené sur 5 (ex. 96 % → 4,8) |
| Ventes | non publiées | ventes des 30 derniers jours |

Les réponses sont **mises en cache** en base (24 h par défaut) ; CJ est limité à 1 requête/seconde. Un produit n'est re-cherché qu'après `matching.recheck_days` jours. Si une source est bloquée ou non configurée, les autres continuent, et un produit n'est marqué *introuvable* que si au moins une source a réellement répondu.

### Import CSV fournisseur

Glissez un CSV dans « Lancer une collecte » ou déposez-le dans `data/supplier_csv/`. Colonnes reconnues (noms FR ou EN) : `title`, `url`, `price`, `shipping`, `delivery` (ex. `7-12`) ou `delivery min` / `delivery max`, `stock`, `rating` (/5 ou %), `orders`, `image`, `supplier`, et `product_id` (id product-hunter, facultatif : sinon rattachement par similarité de titre).

Les colonnes « AliExpress Price / Link » des exports Minea deviennent des offres **partielles** (port et délai inconnus) : à compléter dans la fiche avant qu'elles ne comptent.

## Marge et prix conseillé (`config.yaml` → `economics`)

```
profit net = prix de vente − prix d'achat − livraison − 3 % (paiement) − 25 % (pub estimée)
```

- **Prix plancher** : prix qui garantit la marge nette visée (20 % par défaut), arrondi à x,90.
- **Prix conseillé** : la médiane des prix concurrents si elle est au-dessus du plancher (le marché accepte ce prix), sinon le plancher (signalé : concurrents moins chers).
- Parmi les offres conformes, l'outil retient automatiquement **la moins chère rendue en France** ; vous pouvez en imposer une autre dans la fiche.

## Score /100 (`weights`)

| Critère | Poids | Calcul |
|---|---|---|
| Marge nette | 30 | marge nette % : 5 % → 0, 30 % → 1 |
| Demande | 25 | moyenne des signaux disponibles : Google Trends, nombre d'annonceurs (idéal 3-15), ancienneté des pubs (≥ 30 j), rang best-seller |
| Faible saturation | 15 | nombre de concurrents (boutiques suivies + annonceurs) : 0 à partir de 5 |
| Fiabilité fournisseur | 20 | délai (≤ 7 j = max), note, stock, ventes, qualité de la correspondance |
| Effet wow | 10 | note 0-10 donnée par Claude |

## Interface

- **Produits** : collecte, cartes ou tableau (photo fournisseur, achat + port, prix conseillé, profit net, délai, score, bouton « Fournisseur »), filtres, génération IA du top, sélection et **Exporter vers Shopify**.
- **Fiche produit** : économie unitaire (cascade), toutes les offres fournisseur avec liens directs, choix / saisie manuelle d'une offre, relance de la recherche, concurrents et pubs, score détaillé, Google Trends, fiche IA (aperçu HTML + code).
- **Mes tests** : budget, CA, ROAS, statut (à tester / en test / validé / abandonné).
- **Réglages** : économie et filtres, pondérations, boutiques concurrentes, sources, clés API, mots-clés exclus, YAML brut.

## Export Shopify

Le CSV suit le **modèle actuel** de Shopify (`Title`, `URL handle`, `Description`, `Vendor`, `Type`, `Tags`, `Status`, `Price`, `Cost per item`, `Product image URL`, `SEO title`…, 61 colonnes). Shopify accepte aussi les anciens noms (`Handle`, `Body (HTML)`, `Variant Price`, `Image Src`).

- Prix = prix conseillé ; *Cost per item* = coût rendu (achat + port).
- Image = photo du **fournisseur** (jamais celle d'un concurrent).
- Produits importés en **brouillon** par défaut (`shopify_export.status`).
- Sans fiche IA générée, la description est vide (signalé à l'export).

Import : Shopify admin → Produits → Importer.

## Ligne de commande

```bash
python -m collectors.shopify_stores
python -m collectors.google_trends "lampe lune"
python -m collectors.ads_import examples/exemple_minea.csv
python margins.py
python scoring.py
python pipeline.py shopify ads suppliers
```

## Structure

```
product-hunter/
  app.py                 # interface Streamlit
  pipeline.py            # orchestration d'une collecte
  collectors/            # demande : Shopify, Google Trends, import pubs
  suppliers/
    cj.py                # API CJ Dropshipping v2
    aliexpress.py        # API AliExpress affiliés (signature HMAC-SHA256)
    csv_import.py        # import CSV fournisseur
    matching.py          # recherche + rattachement des offres
  margins.py             # profit net et prix conseillé
  scoring.py             # regroupement, choix d'offre, filtres, score
  analyzer.py            # Claude : requêtes EN + fiches Shopify (cache)
  shopify_export.py      # CSV d'import Shopify
  db.py                  # SQLite (produits, offres, cache API, analyses, tests)
  config.yaml            # seuils, pondérations, sources
```

## Limites

- Les intégrations CJ et AliExpress suivent la documentation officielle (vérifiée en octobre 2026) mais n'ont pas pu être testées avec de vraies clés lors du développement : au premier lancement, surveillez le rapport de collecte.
- Le rapprochement fournisseur est textuel (titres) : vérifiez toujours visuellement l'offre retenue avant de tester.
- La note AliExpress est le taux d'avis positifs du produit, pas une note de boutique.
- Le coût pub (25 %) est une hypothèse de travail : ajustez-le avec vos ROAS réels (page Mes tests).

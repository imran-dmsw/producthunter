# 🎯 product-hunter

Outil de détection de produits **dropshipping niche pour Shopify** (marché France / Europe), avec une interface Streamlit.

Il collecte des produits depuis plusieurs sources, élimine ceux qui ne respectent pas vos critères (prix, marge, poids, catégories à risque), leur attribue un **score /100**, et fait analyser les meilleurs par **Claude** (angle marketing, public cible, accroches, risques, verdict go/no-go).

## Sources de données

| Source | Fichier | Ce qu'elle apporte | Remarques |
|---|---|---|---|
| Boutiques Shopify concurrentes | `collectors/shopify_stores.py` | catalogue complet (`/products.json`), prix, poids, classement best-sellers | certaines boutiques bloquent `/products.json` ; le classement best-seller n'est lisible que sur les thèmes rendus côté serveur |
| Amazon Movers & Shakers (FR) | `collectors/amazon_movers.py` | produits en forte progression | Amazon sert souvent une grille Movers & Shakers **vide aux robots** : l'outil bascule alors automatiquement sur les *Meilleures ventes* de la catégorie. En cas de captcha, la source est ignorée pour la collecte en cours |
| Google Trends | `collectors/google_trends.py` | intérêt de recherche FR/Europe (moyenne + pente) | limité très vite par Google (HTTP 429) : délais, cache 24 h, nombre de mots-clés plafonné par collecte |
| Import CSV publicités | `collectors/ads_import.py` | nombre d'annonceurs, ancienneté des pubs, liens vers les pubs, prix d'achat | exports **Minea**, **PPSPY** ou **Meta Ad Library** : les colonnes sont reconnues automatiquement (voir `examples/`) |

Si une source échoue, la collecte continue avec les autres ; les erreurs sont visibles dans « Dernière collecte ».

## Installation

Prérequis : **Python 3.11+**.

```bash
cd product-hunter
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # puis renseignez ANTHROPIC_API_KEY
```

> Pas de Python 3.11 sur la machine ? Avec [uv](https://docs.astral.sh/uv/) : `uv venv --python 3.11 .venv && uv pip install -r requirements.txt`.

La clé `ANTHROPIC_API_KEY` (console.anthropic.com) n'est nécessaire que pour l'analyse IA. Elle est lue depuis `.env` et ne doit jamais être écrite dans le code ni commitée (`.env` est dans `.gitignore`).

## Lancement

```bash
streamlit run app.py
```

Puis ouvrez http://localhost:8501.

### Premier usage

1. **Réglages → Sources** : remplacez les boutiques d'exemple par les domaines de vos concurrents Shopify, choisissez les catégories Amazon et les zones Google Trends.
2. **Découverte → Lancer la collecte** : cochez les sources, déposez éventuellement vos exports CSV de pubs (ou placez-les dans `data/ads_csv/`).
3. Filtrez le tableau (prix, marge, catégorie, score min), puis **Analyser le top N avec Claude**.
4. Ouvrez une **fiche produit** : score détaillé, courbe Trends, analyse IA, lien fournisseur, pubs. Corrigez le prix d'achat réel et le mot-clé Trends si besoin.
5. **Ajouter à mes tests**, puis suivez budget, CA, ROAS et statut dans **Mes tests**.

## Pages

- **🔎 Découverte** : collecte, tableau trié par score, filtres, analyse IA du top, export CSV.
- **📦 Fiche produit** : infos, score détaillé, graphique Google Trends, analyse IA (mise en cache), lien fournisseur (ou recherche AliExpress), liens vers les pubs + recherche Meta Ad Library / TikTok Creative Center.
- **🧪 Mes tests** : budget dépensé, CA, ROAS calculé, statut (à tester / en test / validé / abandonné), notes, export CSV.
- **⚙️ Réglages** : seuils, pondérations, sources, mots-clés exclus, édition YAML brute. Les commentaires de `config.yaml` sont conservés à l'enregistrement.

## Filtres éliminatoires (`config.yaml` → `filters`)

- Prix de vente entre 30 et 80 €.
- Coefficient prix de vente / prix d'achat ≥ x3 (appliqué seulement si le prix d'achat est connu : CSV importé ou saisi dans la fiche).
- Poids ≤ 1,5 kg (si le poids est connu, ex. Shopify).
- Mots-clés exclus (titre, catégorie, tags, description), par motif : batterie, fragile, électronique complexe, ingéré / cosmétique / santé, réglementé, marques / propriété intellectuelle. La correspondance se fait sur **mots entiers** (« tea » n'exclut pas « steak »).

Les produits exclus restent en base (bouton « Afficher les produits exclus » avec la raison).

> ⚠️ Les mots-clés ne détectent pas tout : une marque absente de la liste (ex. le nom d'une boutique concurrente) n'est pas repérée. Vérifiez toujours la fiche et l'analyse IA avant de tester un produit.

## Score /100 (`config.yaml` → `weights` et `scoring`)

| Critère | Poids | Calcul (note 0 → 1) |
|---|---|---|
| Marge estimée | 25 | coefficient x2 → 0, x5 → 1. Prix d'achat inconnu : estimé à 30 % du prix de vente (`estimated_cost_ratio`) |
| Tendance Google Trends | 20 | ½ intérêt récent (60/100 = max) + ½ pente (atténuée si le volume est très faible) |
| Nombre d'annonceurs | 15 | 1 entre 3 et 15 annonceurs ; en dessous = produit pas encore validé ; au-delà = décroît jusqu'à 0 à 50 |
| Ancienneté des pubs | 15 | jours entre la 1re pub vue et la dernière ; 1 à partir de 30 jours |
| Faible saturation | 15 | nombre de boutiques suivies vendant un titre similaire (ou nombre d'annonceurs) ; 0 à partir de 5 |
| Effet wow / problème résolu | 10 | note 0-10 donnée par Claude |

Une donnée manquante reçoit la note `missing_data_value` (0,3 par défaut) et apparaît en jaune dans la fiche. Le total est ramené sur 100 même si la somme des poids diffère.

## Analyse IA (`analyzer.py`)

- Modèle `claude-opus-5-5` (modifiable dans Réglages), effort `medium`.
- Réponse au format JSON imposé (*structured outputs*) : angle marketing, problème résolu, public cible, 3 accroches, risques (saturation, retours, légal), note wow, prix conseillé, verdict go / no-go / à creuser.
- **Cache** : une analyse est réutilisée pendant `cache_days` (30 j) ; bouton « Ré-analyser » pour forcer.
- **Fallback serveur** activé (`use_fallbacks: true`) : si un filtre de sécurité décline la requête, l'API la rejoue automatiquement sur un modèle de repli. Désactivable dans `config.yaml`.
- Coût indicatif : quelques centimes par produit.

## Ligne de commande

Chaque collecteur peut être testé seul :

```bash
python -m collectors.shopify_stores
python -m collectors.amazon_movers
python -m collectors.google_trends "lampe lune"
python -m collectors.ads_import examples/exemple_minea.csv
python scoring.py                 # recalcule tous les scores
python pipeline.py shopify ads    # collecte sans interface (sources au choix)
```

## Structure

```
product-hunter/
  app.py                 # interface Streamlit (4 pages)
  pipeline.py            # orchestration d'une collecte
  collectors/
    http_client.py       # User-Agent, délais, retries, détection de blocage
    text_utils.py        # nettoyage texte, mots-clés, similarité de titres
    shopify_stores.py    # /products.json + best-selling
    google_trends.py     # pytrends FR/Europe
    amazon_movers.py     # Movers & Shakers (repli Meilleures ventes)
    ads_import.py        # CSV Minea / PPSPY / Meta Ad Library
  scoring.py             # filtres + score /100
  analyzer.py            # analyse IA via l'API Claude (+ cache)
  db.py                  # modèles SQLite (SQLAlchemy)
  settings.py            # lecture/écriture config.yaml et .env
  config.yaml            # seuils et pondérations
  examples/              # CSV d'exemple
  data/                  # base SQLite + dossier d'import CSV
```

## Bonnes pratiques et limites

- Le scraping doit rester raisonnable : respectez les délais configurés et les conditions d'utilisation des sites. Les endpoints publics Shopify et les pages Amazon peuvent changer ou être bloqués à tout moment.
- Les prix Shopify sont convertis en euros avec des taux fixes approximatifs (`FX_TO_EUR` dans `shopify_stores.py`) ; une boutique peut afficher une devise différente selon le pays d'où part la requête.
- Google Trends renvoie des valeurs **relatives** (0-100 par rapport au pic de la période) : comparez les pentes plutôt que les valeurs absolues.
- Le score est une aide au tri, pas une garantie : validez toujours avec un petit budget de test.

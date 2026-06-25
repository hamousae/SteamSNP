#!/usr/bin/env python3
"""
Steam Freebie Sniper v2.0
Détecte quand un jeu Steam payant devient gratuit et envoie une notification Discord.
Nécessite une clé API Steam (gratuite) : https://steamcommunity.com/dev/apikey
"""

import requests
import json
import time
import os
import sys
from datetime import datetime

# ═══════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
SCAN_INTERVAL = 2.5      # heures entre chaque scan
BATCH_SIZE = 50          # apps par requête appdetails (50 pour moins de rate limit)
DELAY = 3.0              # secondes entre chaque batch (évite le rate limit)
CACHE_FILE = "games_cache.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SteamSniper/3.0"
# ═══════════════════════════════════════════════

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")

def get_all_apps():
    """Récupère TOUS les jeux Steam via IStoreService/GetAppList (paginé)."""
    log("Récupération de la liste des jeux Steam...")
    apps = []
    last_appid = 0
    page = 0

    while True:
        params = {
            "key": STEAM_API_KEY,
            "max_results": 50000,
            "last_appid": last_appid,
            "include_games": 1,
            "include_dlc": 0,
            "include_software": 0,
            "include_videos": 0,
            "include_hardware": 0,
        }
        r = SESSION.get(
            "https://api.steampowered.com/IStoreService/GetAppList/v1/",
            params=params,
            timeout=60
        )
        if r.status_code == 403:
            log("ERREUR: Clé API Steam invalide !")
            sys.exit(1)
        r.raise_for_status()
        data = r.json()
        batch = data.get("response", {}).get("apps", [])
        if not batch:
            break
        apps.extend(batch)
        last_appid = batch[-1]["appid"]
        page += 1
        log(f"  Page {page} : {len(batch)} apps (total: {len(apps)})")
        if len(batch) < 50000:
            break
        time.sleep(0.5)

    log(f"Total: {len(apps)} jeux trouvés")
    return apps

def check_batch(app_ids, retry=0):
    """Vérifie les détails (type, prix) d'un batch d'app IDs avec backoff."""
    url = "https://store.steampowered.com/api/appdetails?" + "&".join(f"appids={aid}" for aid in app_ids)
    r = SESSION.get(url, timeout=30)
    if r.status_code == 429:
        wait = min(60 * (2 ** retry), 600)  # 60s, 120s, 240s, 480s, max 600s
        log(f"Rate limit - pause {wait}s (tentative {retry+1})")
        time.sleep(wait)
        return check_batch(app_ids, retry + 1)
    r.raise_for_status()
    return r.json()

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

def send_webhook(payload):
    if not WEBHOOK_URL:
        return
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 204:
            log(f"✓ Webhook envoyé")
        else:
            log(f"✗ Webhook {r.status_code}")
    except Exception as e:
        log(f"✗ Erreur webhook : {e}")

def notify_free(name, appid):
    log(f"🎉 GRATUIT : {name}")
    send_webhook({
        "embeds": [{
            "title": name,
            "url": f"https://store.steampowered.com/app/{appid}",
            "description": "@everyone 🎉 **Ce jeu est maintenant GRATUIT sur Steam !**",
            "color": 5814783,
            "thumbnail": {"url": f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"},
            "footer": {"text": "Steam Freebie Sniper"}
        }]
    })

def notify_start():
    send_webhook({
        "embeds": [{
            "title": "Scan Steam en cours...",
            "description": "@everyone 🔍 Vérification des prix Steam en cours",
            "color": 15105570,
            "footer": {"text": "Steam Freebie Sniper"}
        }]
    })

def scan():
    cache = load_cache()
    apps = get_all_apps()
    notify_start()
    total = len(apps)
    stats = {"checked": 0, "games": 0, "free": 0}
    freebies = []

    # Optimisation : ne vérifier que les jeux payants + nouveaux (les gratuits le restent)
    # Premier scan = tout vérifier ; scans suivants = économie de requêtes
    is_first_scan = not cache
    if is_first_scan:
        log(f"Premier scan - analyse complète de {total} jeux (lent)")
        to_check = apps
    else:
        # Jeux payants ou inconnus uniquement
        to_check = [a for a in apps if str(a["appid"]) not in cache
                    or cache.get(str(a["appid"]), {}).get("price", 0) > 0]
        log(f"Scan rapide - {len(to_check)}/{total} jeux à vérifier "
            f"({total - len(to_check)} gratuits déjà connus, ignorés)")

    checked_count = len(to_check)

    if not to_check:
        log("Rien à vérifier - tous les jeux déjà en cache sont gratuits")
        return []

    for i in range(0, len(to_check), BATCH_SIZE):
        batch = to_check[i:i + BATCH_SIZE]
        ids = [a["appid"] for a in batch]

        try:
            data = check_batch(ids)
        except Exception as e:
            log(f"Erreur batch {i} : {e}")
            time.sleep(5)
            continue

        for app in batch:
            sid = str(app["appid"])
            if sid not in data or not data[sid].get("success"):
                continue
            entry = data[sid]["data"]

            if entry.get("type") != "game":
                continue

            stats["games"] += 1
            price = entry.get("price_overview", {}).get("final", 0)
            if price == 0:
                stats["free"] += 1

            old = cache.get(sid)
            if old and old.get("price", 0) > 0 and price == 0:
                name = app.get("name", "Unknown")
                notify_free(name, app["appid"])
                freebies.append((name, app["appid"]))

            cache[sid] = {
                "name": app.get("name", "Unknown"),
                "price": price,
                "checked": datetime.now().isoformat()
            }

        stats["checked"] += len(batch)

        if (i // BATCH_SIZE) % 50 == 0:
            save_cache(cache)

        pct = (i + len(batch)) / checked_count * 100
        bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
        sys.stdout.write(f"\r  [{bar}] {min(i + BATCH_SIZE, checked_count):>6}/{checked_count} ({pct:5.1f}%) | "
                        f"{stats['games']} jeux, {stats['free']} gratuits")
        sys.stdout.flush()

        time.sleep(DELAY)

    save_cache(cache)
    print()
    log(f"Scan terminé ! {len(freebies)} jeu(x) devenu(s) gratuit(s).")
    log(f"Cache: {len(cache)} jeux dont {stats['free']} gratuits")
    return freebies

def main():
    print("╔══════════════════════════════════════════╗")
    print("║      Steam Freebie Sniper v2.0           ║")
    print("║  Détecte les jeux Steam devenus gratuits ║")
    print("╚══════════════════════════════════════════╝")

    errors = []
    if not STEAM_API_KEY:
        errors.append("STEAM_API_KEY est vide (va sur https://steamcommunity.com/dev/apikey)")
    if not WEBHOOK_URL:
        errors.append("WEBHOOK_URL est vide (configure ton webhook Discord)")

    for err in errors:
        log(f"⚠ {err}")

    if "--once" in sys.argv:
        scan()
        return

    while True:
        debut = time.time()
        scan()
        duree = (time.time() - debut) / 3600
        prochain = SCAN_INTERVAL
        log(f"Scan: {duree:.1f}h | Prochain scan dans {prochain:.1f}h")
        time.sleep(prochain * 3600)

if __name__ == "__main__":
    main()

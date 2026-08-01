"""
HHB Fortbildungszentrum -- Fortbildungs- und Prüfungsportal
Einzeldatei-Backend: Flask-Webserver (Keep-Alive) + Discord-Bot (discord.py)

Benötigte Umgebungsvariablen:
  DISCORD_BOT_TOKEN       Bot-Token
  DISCORD_CLIENT_ID       OAuth2 Client-ID der Discord-Anwendung
  DISCORD_CLIENT_SECRET   OAuth2 Client-Secret
  DISCORD_REDIRECT_URI    z.B. https://deine-domain.tld/callback
  FLASK_SECRET_KEY        beliebiger zufälliger String (optional, wird sonst generiert)
  PORT                    optional, Standard 8080
"""

import asyncio
import io
import json
import os
import random
import string
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, jsonify, redirect, request, send_from_directory, session

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DISCORD_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
DISCORD_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32).hex()
PORT = int(os.environ.get("PORT", 8080))

# Basis-URL des Webportals, für Link-Buttons in Bot-Nachrichten (aus der Redirect-URI abgeleitet)
PUBLIC_URL = DISCORD_REDIRECT_URI[: -len("/callback")] if DISCORD_REDIRECT_URI.endswith("/callback") else DISCORD_REDIRECT_URI

DISCORD_API = "https://discord.com/api/v10"

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "database"
DB_DIR.mkdir(exist_ok=True)

DB_FILES = ["users", "tests", "questions", "results", "codes", "settings", "logs"]
DB_LOCK = threading.RLock()

ROLE_ORDER = {"mitarbeiter": 1, "fortbilder": 2, "fortbildungsleitung": 3}

GRADING_SCALE = [
    (98, 100, "1+"), (95, 97, "1"), (92, 94, "1-"),
    (89, 91, "2+"), (85, 88, "2"), (80, 84, "2-"),
    (75, 79, "3+"), (70, 74, "3"), (65, 69, "3-"),
    (60, 64, "4"), (55, 59, "4-"), (50, 54, "5+"),
    (45, 49, "5"), (35, 44, "5-"), (0, 34, "6"),
]
PASS_PERCENT = 60


def grade_for_percent(pct):
    pct_r = max(0, min(100, round(pct)))
    for lo, hi, note in GRADING_SCALE:
        if lo <= pct_r <= hi:
            return note
    return "6"


# ---------------------------------------------------------------------------
# Seed-Daten (aus den Fortbildungsunterlagen "Auszubildende" und "Kredite")
# ---------------------------------------------------------------------------

SEED_TESTS = {
    "test_1": {
        "id": "test_1",
        "title": "Fortbildung für Auszubildende",
        "time_limit_minutes": 25,
        "max_points": 53,
        "content": [
            {
                "chapter": "Kapitel 1 - Allgemeines Wissen",
                "sections": [
                    {
                        "title": "Verhalten als Bankberater",
                        "text": "Lest unbedingt das Handbuch in #berater-handbuch durch. Als "
                                 "Bankberater müsst ihr jederzeit die Schweigepflicht beachten "
                                 "und immer freundlich gegenüber den Kunden sein. Sagt immer nur "
                                 "Dinge, die ihr zu 100% wisst, um keine Missverständnisse zu "
                                 "verursachen."
                    },
                    {
                        "title": "Arbeiten im Roleplay",
                        "text": "Kommt niemals hinter den Schalter, auch wenn eine Person Hilfe "
                                 "am Automaten benötigt. Bleibt immer in der Nähe des "
                                 "Panik-Buttons (Notruf senden). Schliesst die Türen mit Kegeln "
                                 "ab, damit sich niemand unbefugt Zutritt verschaffen kann."
                    },
                ],
            },
            {
                "chapter": "Kapitel 2 - Bearbeiten von Tickets",
                "sections": [
                    {
                        "title": "Tickets claimen",
                        "text": "Um ein Ticket zu claimen, klickt ihr auf den Button 'Claim'. "
                                 "Wenn ihr das Ticket erfolgreich geclaimt habt, erscheint eine "
                                 "Nachricht mit eurem Ping."
                    },
                    {
                        "title": "Tickets bearbeiten",
                        "text": "Begrüsst den Kunden freundlich, wartet auf einen Zahlungslink "
                                 "(bei Auszahlungen nicht nötig), führt den jeweiligen Befehl "
                                 "aus und sendet dem Kunden einen Screenshot als Arbeitsnachweis."
                    },
                    {
                        "title": "Tickets abschliessen",
                        "text": "Fragt den Kunden, ob ihr noch etwas für ihn tun könnt. Führt "
                                 "/ticket requestclose aus und stellt 12 Stunden Timeout ein. "
                                 "Dokumentiert eure Arbeit anschliessend in #dokumentation."
                    },
                ],
            },
            {
                "chapter": "Kapitel 3 - Umgang mit Kunden",
                "sections": [
                    {
                        "title": "Kundenservice",
                        "text": "Freundliches Verhalten gegenüber Kunden, Kunden immer siezen, "
                                 "jeder Kunde wird gleich behandelt. Bei Fragen ehrlich antworten, "
                                 "im Zweifel helfen andere Bankberater gerne aus."
                    },
                    {
                        "title": "Schwierige Kunden",
                        "text": "Nehmt nicht alles persönlich, kommuniziert sachlich und fallt "
                                 "dem Kunden nicht ins Wort. Bei anhaltendem Fehlverhalten kann "
                                 "vom Hausrecht Gebrauch gemacht werden."
                    },
                    {
                        "title": "Hausverbot",
                        "text": "Ein Hausverbot muss im Kanal #hausverbote dokumentiert und mit "
                                 "/hausverbot ausgesprochen werden. Mitarbeiter dürfen maximal 7 "
                                 "Tage aussprechen, bei schwerwiegenden Verstössen wird die "
                                 "Leitungsebene kontaktiert."
                    },
                ],
            },
        ],
        "question_order": ["t1_mc1", "t1_mc2", "t1_mc3", "t1_mc4", "t1_mc5", "t1_mc6",
                            "t1_tf1", "t1_tf2", "t1_tf3", "t1_tf4", "t1_tf5",
                            "t1_open1", "t1_open2", "t1_praxis1"],
        "prerequisite_test_id": None,
    },
    "test_2": {
        "id": "test_2",
        "title": "Fortbildung für Kredite",
        "time_limit_minutes": 35,
        "max_points": 71,
        "content": [
            {
                "chapter": "Kapitel 1 - Grundlagen des Kreditsystems",
                "sections": [
                    {
                        "title": "Was ist ein Kredit?",
                        "text": "Ein Kredit ist die zeitweise Ueberlassung von Geld durch einen "
                                 "Kreditgeber an einen Kreditnehmer. Der Empfänger verpflichtet "
                                 "sich, den Betrag zu einem festgelegten Zeitpunkt oder in Raten "
                                 "zurückzuzahlen, wofür die Bank zusätzliche Zinsen und "
                                 "Gebühren erhebt."
                    },
                    {
                        "title": "Normaler Kredit",
                        "text": "Einfacher Kredit ohne festen Tilgungsplan: flexible "
                                 "Rückzahlung, keine festen Monatsraten, geeignet für kleinere "
                                 "Finanzierungen."
                    },
                    {
                        "title": "Ratendarlehen",
                        "text": "Kredit mit festen Tilgungsraten. Monatlich gleich hohe Tilgung, "
                                 "Zinsen werden auf die Restschuld berechnet, die monatliche "
                                 "Belastung sinkt leicht. Vorteile: gute Planbarkeit, sinkende "
                                 "Zinskosten."
                    },
                    {
                        "title": "Annuitätendarlehen",
                        "text": "Kredit mit fester Gesamtrate aus Tilgung und Zinsen. Am Anfang "
                                 "zahlt der Kunde viele Zinsen, mit der Zeit sinken die Zinsen und "
                                 "der Tilgungsanteil steigt. Besonders gut planbar."
                    },
                    {
                        "title": "Ablauf Kredit ausstellen",
                        "text": "Kunde beantragt Kredit -> Kreditvertrag wird in "
                                 "#kredit-verträge gesendet und die Leitungsebene gepingt -> "
                                 "Kredit wird eingetragen -> Kredit-ID erscheint im System -> "
                                 "Auszahlung erfolgt."
                    },
                ],
            },
            {
                "chapter": "Kapitel 2 - Kreditvergabe und Verwaltung",
                "sections": [
                    {
                        "title": "Kredit anlegen",
                        "text": "Vertragsvorlage aus #berater-handbuch kopieren und ausfüllen, "
                                 "vom Kunden unterschreiben lassen, mit /kredit_eintragen "
                                 "eintragen, Nachricht mit Kunde und Vertrag als PDF in "
                                 "#kredit-verträge senden. Auszahlung erfolgt durch die "
                                 "Auszahlungszuständigen."
                    },
                    {
                        "title": "Kreditinformationen abrufen",
                        "text": "Mit /kredit_info erhaltet ihr jederzeit Kredit-ID, Kredittyp, "
                                 "Kreditsumme, Restschuld, nächste Rate und letzte Zahlungen."
                    },
                ],
            },
            {
                "chapter": "Kapitel 3 - Rückzahlung, Mahnwesen, Sonderfälle",
                "sections": [
                    {
                        "title": "Kreditraten bezahlen",
                        "text": "Rate mit /kredit_zahlen abbuchen. Nach jeder Zahlung sinkt die "
                                 "Restschuld, die Zahlung wird dokumentiert, der Kredit schliesst "
                                 "nach vollständiger Tilgung automatisch."
                    },
                    {
                        "title": "Mindestlaufzeit",
                        "text": "Jeder Kredit hat eine Mindestlaufzeit von 80% der vereinbarten "
                                 "Zeit, eine vorzeitige Beendigung ist vorher nicht möglich. Nach "
                                 "der Mindestlaufzeit ist vollständige Rückzahlung möglich, "
                                 "jedoch mit einem Aufschlag von 15% für entgangene Zinsen."
                    },
                    {
                        "title": "Mahnverfahren",
                        "text": "1. Zahlungsverzug -> Zahlungserinnerung. 2. Zahlungsverzug -> "
                                 "Mahnung. 3. Zahlungsverzug -> gesamte Restschuld wird fällig, "
                                 "Vollstreckung bzw. Einzug des Geldes."
                    },
                ],
            },
        ],
        "question_order": ["t2_mc1", "t2_mc2", "t2_mc3", "t2_mc4", "t2_mc5", "t2_mc6",
                            "t2_tf1", "t2_tf2", "t2_tf3", "t2_tf4", "t2_tf5",
                            "t2_open1", "t2_open2", "t2_open3", "t2_praxis1"],
        "prerequisite_test_id": "test_1",
    },
}

SEED_QUESTIONS = {
    "t1_mc1": {"id": "t1_mc1", "test_id": "test_1", "type": "mc", "points": 2,
               "text": "Was ist als Bankberater besonders wichtig?",
               "options": ["Schweigepflicht beachten", "Kunden duzen", "Vermutungen äussern"],
               "correct": [0]},
    "t1_mc2": {"id": "t1_mc2", "test_id": "test_1", "type": "mc", "points": 2,
               "text": "Was ist der erste Schritt bei einem Ticket?",
               "options": ["Ticket claimen", "Ticket schliessen", "Screenshot senden"],
               "correct": [0]},
    "t1_mc3": {"id": "t1_mc3", "test_id": "test_1", "type": "mc", "points": 2,
               "text": "Wie werden Kunden angesprochen?",
               "options": ["Siezen", "Duzen"], "correct": [0]},
    "t1_mc4": {"id": "t1_mc4", "test_id": "test_1", "type": "mc", "points": 3,
               "text": "Wie lange wird nach /ticket requestclose der Timeout eingestellt?",
               "options": ["1 Stunde", "6 Stunden", "12 Stunden", "24 Stunden"],
               "correct": [2]},
    "t1_mc5": {"id": "t1_mc5", "test_id": "test_1", "type": "mc", "points": 3,
               "text": "Wo müssen Hausverbote dokumentiert werden?",
               "options": ["#dokumentation", "#hausverbote", "#berater-handbuch"],
               "correct": [1]},
    "t1_mc6": {"id": "t1_mc6", "test_id": "test_1", "type": "mc", "points": 3,
               "text": "Wie lange dürfen Mitarbeiter maximal selbst ein Hausverbot aussprechen?",
               "options": ["3 Tage", "5 Tage", "7 Tage", "Unbegrenzt"],
               "correct": [2]},
    "t1_tf1": {"id": "t1_tf1", "test_id": "test_1", "type": "tf", "points": 2,
               "text": "Jedes Ticket muss zuerst geclaimt werden.", "correct": True},
    "t1_tf2": {"id": "t1_tf2", "test_id": "test_1", "type": "tf", "points": 2,
               "text": "Nach jedem Ticket muss dokumentiert werden.", "correct": True},
    "t1_tf3": {"id": "t1_tf3", "test_id": "test_1", "type": "tf", "points": 2,
               "text": "Mitarbeiter dürfen Hausverbote unbegrenzt aussprechen.", "correct": False},
    "t1_tf4": {"id": "t1_tf4", "test_id": "test_1", "type": "tf", "points": 2,
               "text": "Man darf hinter dem Schalter hervorkommen, um einem Kunden am Automaten zu helfen.",
               "correct": False},
    "t1_tf5": {"id": "t1_tf5", "test_id": "test_1", "type": "tf", "points": 2,
               "text": "Bei einem schwerwiegenden Verstoß muss die Leitungsebene kontaktiert werden.",
               "correct": True},
    "t1_open1": {"id": "t1_open1", "test_id": "test_1", "type": "open", "points": 8,
                 "text": "Beschreibe den Ablauf eines Tickets."},
    "t1_open2": {"id": "t1_open2", "test_id": "test_1", "type": "open", "points": 8,
                 "text": "Nenne vier Verhaltensregeln für Bankberater."},
    "t1_praxis1": {"id": "t1_praxis1", "test_id": "test_1", "type": "praxis", "points": 12,
                   "text": "Ein Kunde verhält sich aggressiv. Wie gehst du vor?"},

    "t2_mc1": {"id": "t2_mc1", "test_id": "test_2", "type": "mc", "points": 3,
               "text": "Was ist ein Kredit?",
               "options": [
                   "Zeitweise Überlassung von Geld gegen Rückzahlung mit Zinsen",
                   "Ein Geschenk der Bank an den Kunden",
                   "Eine Einzahlung des Kunden auf sein Konto",
               ], "correct": [0]},
    "t2_mc2": {"id": "t2_mc2", "test_id": "test_2", "type": "mc", "points": 3,
               "text": "Welche drei Kreditarten gibt es?",
               "options": [
                   "Normaler Kredit, Ratendarlehen, Annuitätendarlehen",
                   "Girokredit, Tagesgeld, Festgeld",
                   "Basiskredit, Expresskredit, Sofortkredit",
               ], "correct": [0]},
    "t2_mc3": {"id": "t2_mc3", "test_id": "test_2", "type": "mc", "points": 3,
               "text": "Mit welchem Command wird ein Kredit eingetragen?",
               "options": ["/kredit_eintragen", "/kredit_erstellen", "/kredit_anlegen"],
               "correct": [0]},
    "t2_mc4": {"id": "t2_mc4", "test_id": "test_2", "type": "mc", "points": 3,
               "text": "Welche Kreditart eignet sich laut Vergleich besonders für Firmen?",
               "options": ["Normaler Kredit", "Ratendarlehen", "Annuitätendarlehen"],
               "correct": [1]},
    "t2_mc5": {"id": "t2_mc5", "test_id": "test_2", "type": "mc", "points": 3,
               "text": "Welche Kreditart eignet sich laut Vergleich besonders für Privatpersonen?",
               "options": ["Normaler Kredit", "Ratendarlehen", "Annuitätendarlehen"],
               "correct": [2]},
    "t2_mc6": {"id": "t2_mc6", "test_id": "test_2", "type": "mc", "points": 3,
               "text": "Wer ist für die Auszahlung eines Kredits zuständig?",
               "options": ["Der Berater selbst", "Die Leitungsebene", "Die Auszahlungszuständigen", "Der Kunde"],
               "correct": [2]},
    "t2_tf1": {"id": "t2_tf1", "test_id": "test_2", "type": "tf", "points": 3,
               "text": "Die Mindestlaufzeit beträgt 80%.", "correct": True},
    "t2_tf2": {"id": "t2_tf2", "test_id": "test_2", "type": "tf", "points": 3,
               "text": "Nach vollständiger Tilgung schliesst der Kredit automatisch.",
               "correct": True},
    "t2_tf3": {"id": "t2_tf3", "test_id": "test_2", "type": "tf", "points": 3,
               "text": "Nach dem dritten Zahlungsverzug wird die gesamte Restschuld fällig.",
               "correct": True},
    "t2_tf4": {"id": "t2_tf4", "test_id": "test_2", "type": "tf", "points": 3,
               "text": "Ein Kreditvertrag muss vom Kunden unterschrieben werden.",
               "correct": True},
    "t2_tf5": {"id": "t2_tf5", "test_id": "test_2", "type": "tf", "points": 3,
               "text": "Nach der Mindestlaufzeit von 80% ist eine vollständige Rückzahlung ohne Aufschlag möglich.",
               "correct": False},
    "t2_open1": {"id": "t2_open1", "test_id": "test_2", "type": "open", "points": 10,
                 "text": "Beschreibe den Ablauf einer Kreditvergabe."},
    "t2_open2": {"id": "t2_open2", "test_id": "test_2", "type": "open", "points": 10,
                 "text": "Erkläre das Mahnverfahren."},
    "t2_open3": {"id": "t2_open3", "test_id": "test_2", "type": "open", "points": 6,
                 "text": "Welche Informationen zeigt /kredit_info an?"},
    "t2_praxis1": {"id": "t2_praxis1", "test_id": "test_2", "type": "praxis", "points": 12,
                   "text": "Ein Kunde möchte seinen Kredit nach 50% der Laufzeit vollständig "
                           "zurückzahlen. Wie reagierst du?"},
}

DEFAULT_DB = {
    "users": {},
    "tests": SEED_TESTS,
    "questions": SEED_QUESTIONS,
    "results": {},
    "codes": {},
    "settings": {
        "guild_id": "",
        "fortbildungsleitung_role_id": "",
        "fortbilder_role_id": "",
        "mitarbeiter_role_id": "",
        "backup_channel_id": "",
        "review_channel_id": "",
    },
    "logs": [],
}


# ---------------------------------------------------------------------------
# JSON-Datenbank (atomare Schreibvorgänge)
# ---------------------------------------------------------------------------

def db_path(name):
    return DB_DIR / f"{name}.json"


def save_db(name, data):
    path = db_path(name)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_db(name):
    with DB_LOCK:
        path = db_path(name)
        if not path.exists():
            save_db(name, DEFAULT_DB[name])
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def update_db(name, mutate_fn):
    """Lädt, wendet mutate_fn(data) an und speichert wieder -- unter Lock."""
    with DB_LOCK:
        path = db_path(name)
        if not path.exists():
            save_db(name, DEFAULT_DB[name])
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = mutate_fn(data)
        save_db(name, data)
        return result if result is not None else data


def init_db():
    for name in DB_FILES:
        if not db_path(name).exists():
            save_db(name, DEFAULT_DB[name])


def log_event(kind, **fields):
    def mutate(data):
        data.append({"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields})
        if len(data) > 5000:
            del data[: len(data) - 5000]
    update_db("logs", mutate)


init_db()


# ---------------------------------------------------------------------------
# Discord OAuth2 / REST-Hilfsfunktionen
# ---------------------------------------------------------------------------

def oauth_authorize_url():
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "prompt": "none",
    }
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"


def oauth_exchange_code(code):
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(f"{DISCORD_API}/oauth2/token", data=data, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def discord_get_me(access_token):
    r = requests.get(f"{DISCORD_API}/users/@me",
                      headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def bot_get_member(guild_id, user_id):
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    r = requests.get(f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}",
                      headers=headers, timeout=10)
    if r.status_code != 200:
        return None
    return r.json()


def determine_role(member_role_ids):
    settings = load_db("settings")
    role_ids = set(member_role_ids)
    if settings.get("fortbildungsleitung_role_id") and settings["fortbildungsleitung_role_id"] in role_ids:
        return "fortbildungsleitung"
    if settings.get("fortbilder_role_id") and settings["fortbilder_role_id"] in role_ids:
        return "fortbilder"
    if settings.get("mitarbeiter_role_id") and settings["mitarbeiter_role_id"] in role_ids:
        return "mitarbeiter"
    return None


def build_components_v2_payload(title, body_lines, link_url=None, link_label="Im Portal ansehen"):
    """Baut den rohen REST-JSON-Payload einer Components-V2-Nachricht (Titel + Textblöcke
    in einem Container, optional ein Link-Button darunter)."""
    container_components = [{"type": 10, "content": f"# {title}"}, {"type": 14}]
    for i, line in enumerate(body_lines):
        if i > 0:
            container_components.append({"type": 14})
        container_components.append({"type": 10, "content": line})

    components = [{"type": 17, "components": container_components}]
    if link_url:
        components.append({
            "type": 1,
            "components": [{"type": 2, "style": 5, "label": link_label, "url": link_url}],
        })
    return {"flags": 1 << 15, "components": components}


def bot_send_dm(user_id, title, body_lines, link_url=None, link_label="Im Portal ansehen"):
    """Schickt einem Discord-User eine Direktnachricht (Components V2) per REST-API."""
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    try:
        r = requests.post(f"{DISCORD_API}/users/@me/channels",
                           json={"recipient_id": str(user_id)}, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            return False
        channel_id = r.json()["id"]
        payload = build_components_v2_payload(title, body_lines, link_url, link_label)
        r2 = requests.post(f"{DISCORD_API}/channels/{channel_id}/messages",
                            json=payload, headers=headers, timeout=10)
        return r2.status_code in (200, 201)
    except requests.RequestException:
        return False


def bot_send_channel_message(channel_id, title, body_lines, link_url=None, link_label="Im Portal ansehen"):
    if not channel_id:
        return False
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    payload = build_components_v2_payload(title, body_lines, link_url, link_label)
    try:
        r = requests.post(f"{DISCORD_API}/channels/{channel_id}/messages",
                           json=payload, headers=headers, timeout=10)
        return r.status_code in (200, 201)
    except requests.RequestException:
        return False


def has_passed(results, user_id, test_id):
    return any(
        r["user_id"] == user_id and r["test_id"] == test_id
        and r["status"] == "bewertet" and (r.get("percent") or 0) >= PASS_PERCENT
        for r in results.values()
    )


# ---------------------------------------------------------------------------
# Flask-App
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)
app.secret_key = FLASK_SECRET_KEY
app.config.update(SESSION_COOKIE_SAMESITE="Lax")


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    users = load_db("users")
    u = users.get(uid)
    if not u:
        return None
    return {"id": uid, **u}


def require_role(min_role):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                return jsonify({"error": "not_authenticated"}), 401
            if ROLE_ORDER.get(u["role"], 0) < ROLE_ORDER[min_role]:
                return jsonify({"error": "forbidden"}), 403
            request.current_user = u
            return fn(*args, **kwargs)
        return wrapper
    return deco


# ----- Statische Seite -----

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


# ----- Auth -----

@app.route("/login")
def login():
    return redirect(oauth_authorize_url())


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?error=oauth")
    try:
        token_data = oauth_exchange_code(code)
        me = discord_get_me(token_data["access_token"])
    except Exception:
        return redirect("/?error=oauth")

    settings = load_db("settings")
    guild_id = settings.get("guild_id")
    if not guild_id:
        return redirect("/?error=not_configured")

    member = bot_get_member(guild_id, me["id"])
    if not member:
        return redirect("/?error=not_member")

    role = determine_role(member.get("roles", []))
    if not role:
        return redirect("/?error=no_role")

    now = datetime.now(timezone.utc).isoformat()
    display_name = member.get("nick") or me.get("global_name") or me["username"]

    def mutate(users):
        entry = users.get(me["id"], {"first_login": now})
        entry.update({"username": display_name, "role": role, "last_login": now})
        users[me["id"]] = entry

    update_db("users", mutate)
    session["user_id"] = me["id"]
    log_event("login", user_id=me["id"], role=role)
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/api/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "id": u["id"], "username": u["username"], "role": u["role"]})


# ----- Fortbildungen (Lerninhalte) -----

@app.route("/api/trainings")
@require_role("mitarbeiter")
def api_trainings():
    tests = load_db("tests")
    return jsonify([
        {"id": t["id"], "title": t["title"], "time_limit_minutes": t["time_limit_minutes"],
         "max_points": t["max_points"]}
        for t in tests.values()
    ])


@app.route("/api/trainings/<test_id>")
@require_role("mitarbeiter")
def api_training_detail(test_id):
    tests = load_db("tests")
    t = tests.get(test_id)
    if not t:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"id": t["id"], "title": t["title"], "content": t["content"],
                     "time_limit_minutes": t["time_limit_minutes"], "max_points": t["max_points"]})


# ----- Einmalcodes -----

def gen_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


@app.route("/api/codes", methods=["GET", "POST"])
@require_role("fortbilder")
def api_codes():
    u = request.current_user
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        test_id = body.get("test_id")
        tests = load_db("tests")
        if test_id not in tests:
            return jsonify({"error": "invalid_test"}), 400

        def mutate(codes):
            code = gen_code()
            while code in codes:
                code = gen_code()
            codes[code] = {
                "test_id": test_id, "created_by": u["id"], "created_by_name": u["username"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "used": False, "used_by": None, "used_at": None,
            }
            return code

        code = update_db("codes", mutate)
        return jsonify({"code": code, "test_id": test_id})

    codes = load_db("codes")
    mine = {c: v for c, v in codes.items() if v["created_by"] == u["id"]}
    return jsonify(mine)


# ----- Testablauf (Mitarbeiter) -----

@app.route("/api/code/redeem", methods=["POST"])
@require_role("mitarbeiter")
def api_code_redeem():
    u = request.current_user
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip().upper()

    codes = load_db("codes")
    entry = codes.get(code)
    if not entry:
        return jsonify({"error": "invalid_code"}), 404
    if entry["used"]:
        return jsonify({"error": "code_used"}), 400

    tests = load_db("tests")
    test = tests.get(entry["test_id"])
    if not test:
        return jsonify({"error": "test_missing"}), 400

    prereq_id = test.get("prerequisite_test_id")
    if prereq_id:
        results = load_db("results")
        if not has_passed(results, u["id"], prereq_id):
            prereq_test = tests.get(prereq_id)
            prereq_title = prereq_test["title"] if prereq_test else prereq_id
            return jsonify({"error": "prerequisite_not_met", "prerequisite_title": prereq_title}), 400

    def mutate_codes(c):
        c[code]["used"] = True
        c[code]["used_by"] = u["id"]
        c[code]["used_at"] = datetime.now(timezone.utc).isoformat()

    update_db("codes", mutate_codes)

    attempt_id = f"r_{int(datetime.now(timezone.utc).timestamp())}_{u['id']}"

    result = {
        "id": attempt_id, "user_id": u["id"], "username": u["username"],
        "test_id": test["id"], "test_title": test["title"], "code": code,
        "time_limit_minutes": test["time_limit_minutes"],
        "answers": {}, "auto_score": 0, "manual_score": None, "manual_points": {},
        "question_comments": {},
        "total_score": None, "max_points": test["max_points"], "percent": None, "grade": None,
        "status": "bereit", "graded_by": None, "comment": "",
        "started_at": None, "deadline": None,
        "submitted_at": None, "graded_at": None, "released": False,
        "viewed_at": None,
    }

    def mutate_results(r):
        r[attempt_id] = result

    update_db("results", mutate_results)
    return jsonify({"attempt_id": attempt_id})


def public_questions_for(test_id):
    questions = load_db("questions")
    tests = load_db("tests")
    order = tests[test_id]["question_order"]
    out = []
    for qid in order:
        q = questions[qid]
        pub = {"id": q["id"], "type": q["type"], "text": q["text"], "points": q["points"]}
        if q["type"] == "mc":
            pub["options"] = q["options"]
        out.append(pub)
    return out


@app.route("/api/attempt/<attempt_id>")
@require_role("mitarbeiter")
def api_attempt(attempt_id):
    u = request.current_user
    results = load_db("results")
    r = results.get(attempt_id)
    if not r or r["user_id"] != u["id"]:
        return jsonify({"error": "not_found"}), 404
    if r["status"] == "bereit":
        return jsonify({
            "id": r["id"], "test_id": r["test_id"], "test_title": r["test_title"],
            "status": "bereit", "time_limit_minutes": r["time_limit_minutes"],
        })
    if r["status"] == "in_bearbeitung":
        return jsonify({
            "id": r["id"], "test_id": r["test_id"], "test_title": r["test_title"],
            "status": "in_bearbeitung", "deadline": r["deadline"], "answers": r["answers"],
            "questions": public_questions_for(r["test_id"]),
        })
    return jsonify({"id": r["id"], "test_title": r["test_title"], "status": r["status"]})


@app.route("/api/attempt/<attempt_id>/start", methods=["POST"])
@require_role("mitarbeiter")
def api_attempt_start(attempt_id):
    u = request.current_user

    def mutate(results):
        r = results.get(attempt_id)
        if not r or r["user_id"] != u["id"]:
            return {"error": "not_found"}
        if r["status"] != "bereit":
            return {"error": "already_started"}
        started_at = datetime.now(timezone.utc)
        deadline = started_at + timedelta(minutes=r["time_limit_minutes"])
        r["status"] = "in_bearbeitung"
        r["started_at"] = started_at.isoformat()
        r["deadline"] = deadline.isoformat()
        return {
            "ok": True, "deadline": r["deadline"], "test_title": r["test_title"],
            "questions": public_questions_for(r["test_id"]),
        }

    out = update_db("results", mutate)
    if "error" in out:
        return jsonify(out), 400
    return jsonify(out)


@app.route("/api/attempt/<attempt_id>/save", methods=["POST"])
@require_role("mitarbeiter")
def api_attempt_save(attempt_id):
    u = request.current_user
    body = request.get_json(force=True, silent=True) or {}
    answers = body.get("answers", {})

    def mutate(results):
        r = results.get(attempt_id)
        if not r or r["user_id"] != u["id"] or r["status"] != "in_bearbeitung":
            return False
        r["answers"].update(answers)
        return True

    ok = update_db("results", mutate)
    if ok is False:
        return jsonify({"error": "not_found_or_closed"}), 400
    return jsonify({"saved": True})


@app.route("/api/attempt/<attempt_id>/submit", methods=["POST"])
@require_role("mitarbeiter")
def api_attempt_submit(attempt_id):
    u = request.current_user
    body = request.get_json(force=True, silent=True) or {}
    answers = body.get("answers", {})
    questions = load_db("questions")

    def mutate(results):
        r = results.get(attempt_id)
        if not r or r["user_id"] != u["id"]:
            return {"error": "not_found"}
        if r["status"] != "in_bearbeitung":
            return {"error": "already_submitted"}
        r["answers"].update(answers)

        auto_score = 0
        needs_manual = False
        for qid, q in questions.items():
            if q["test_id"] != r["test_id"]:
                continue
            given = r["answers"].get(qid)
            if q["type"] == "mc":
                correct = set(q["correct"])
                given_set = set(given) if isinstance(given, list) else set()
                if given_set == correct:
                    auto_score += q["points"]
            elif q["type"] == "tf":
                if isinstance(given, bool) and given == q["correct"]:
                    auto_score += q["points"]
            else:
                needs_manual = True

        r["auto_score"] = auto_score
        r["submitted_at"] = datetime.now(timezone.utc).isoformat()

        if needs_manual:
            r["status"] = "eingereicht"
        else:
            r["manual_score"] = 0
            total = auto_score
            pct = (total / r["max_points"]) * 100 if r["max_points"] else 0
            r["total_score"] = total
            r["percent"] = round(pct, 1)
            r["grade"] = grade_for_percent(pct)
            r["status"] = "bewertet"
            r["graded_at"] = r["submitted_at"]
            r["released"] = True
        return {
            "ok": True, "status": r["status"], "user_id": r["user_id"], "test_id": r["test_id"],
            "test_title": r["test_title"], "username": r["username"],
            "percent": r.get("percent"), "grade": r.get("grade"),
        }

    out = update_db("results", mutate)
    if "error" in out:
        return jsonify(out), 400
    log_event("test_submitted", user_id=u["id"], attempt_id=attempt_id)

    settings = load_db("settings")
    if out["status"] == "eingereicht":
        fortbilder_role = settings.get("fortbilder_role_id")
        review_channel = settings.get("review_channel_id")
        if review_channel and fortbilder_role:
            bot_send_channel_message(
                review_channel,
                "Neue Prüfung wartet auf Bewertung",
                [f"<@&{fortbilder_role}>", f"**{out['username']}** hat **{out['test_title']}** eingereicht."],
                link_url=PUBLIC_URL,
                link_label="Zur Bewertung",
            )
    elif out["status"] == "bewertet":
        bot_send_dm(
            out["user_id"],
            "Deine Prüfung wurde bewertet",
            [f"**{out['test_title']}**\nNote {out['grade']} ({out['percent']}%)"],
            link_url=PUBLIC_URL,
            link_label="Ergebnis ansehen",
        )

    return jsonify(out)


@app.route("/api/results/mine")
@require_role("mitarbeiter")
def api_results_mine():
    u = request.current_user
    results = load_db("results")
    mine = [r for r in results.values() if r["user_id"] == u["id"] and r["status"] not in ("in_bearbeitung", "bereit")]
    mine.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    visible = [r for r in mine if r["released"]]
    summary = [{
        "id": r["id"], "test_id": r["test_id"], "test_title": r["test_title"],
        "status": r["status"], "percent": r["percent"], "grade": r["grade"],
        "total_score": r["total_score"], "max_points": r["max_points"],
        "submitted_at": r["submitted_at"], "comment": r["comment"],
        "viewed": r.get("viewed_at") is not None,
    } for r in visible]
    return jsonify(summary)


@app.route("/api/results/mine/<result_id>/view", methods=["POST"])
@require_role("mitarbeiter")
def api_results_mine_view(result_id):
    u = request.current_user
    questions = load_db("questions")

    def mutate(results):
        r = results.get(result_id)
        if not r or r["user_id"] != u["id"] or not r["released"]:
            return {"error": "not_found"}
        if r.get("viewed_at") is not None:
            return {"error": "already_viewed"}
        r["viewed_at"] = datetime.now(timezone.utc).isoformat()
        qs = [questions[qid] for qid in load_db("tests")[r["test_id"]]["question_order"] if qid in questions]
        view_expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        return {"ok": True, "result": dict(r), "questions": qs, "view_expires_at": view_expires}

    out = update_db("results", mutate)
    if "error" in out:
        return jsonify(out), 400 if out["error"] == "already_viewed" else 404
    return jsonify(out)


# ----- Bewertung (Fortbilder) -----

@app.route("/api/results/pending")
@require_role("fortbilder")
def api_results_pending():
    results = load_db("results")
    pending = [r for r in results.values() if r["status"] == "eingereicht"]
    pending.sort(key=lambda r: r.get("submitted_at") or "")
    return jsonify(pending)


@app.route("/api/results/<result_id>")
@require_role("fortbilder")
def api_result_detail(result_id):
    results = load_db("results")
    r = results.get(result_id)
    if not r:
        return jsonify({"error": "not_found"}), 404
    questions = load_db("questions")
    qs = [questions[qid] for qid in load_db("tests")[r["test_id"]]["question_order"]]
    return jsonify({"result": r, "questions": qs})


@app.route("/api/results/<result_id>/grade", methods=["POST"])
@require_role("fortbilder")
def api_result_grade(result_id):
    u = request.current_user
    body = request.get_json(force=True, silent=True) or {}
    manual_points = body.get("manual_points", {})
    question_comments = body.get("question_comments", {})
    comment = body.get("comment", "")
    questions = load_db("questions")

    def mutate(results):
        r = results.get(result_id)
        if not r:
            return {"error": "not_found"}
        if r["status"] != "eingereicht":
            return {"error": "not_gradable"}

        manual_total = 0
        manual_breakdown = {}
        comment_breakdown = {}
        for qid, q in questions.items():
            if q["test_id"] != r["test_id"] or q["type"] not in ("open", "praxis"):
                continue
            pts = manual_points.get(qid, 0)
            try:
                pts = float(pts)
            except (TypeError, ValueError):
                pts = 0
            pts = max(0, min(pts, q["points"]))
            manual_breakdown[qid] = pts
            manual_total += pts
            note = (question_comments.get(qid) or "").strip()
            if note:
                comment_breakdown[qid] = note

        total = r["auto_score"] + manual_total
        pct = (total / r["max_points"]) * 100 if r["max_points"] else 0

        r["manual_score"] = manual_total
        r["manual_points"] = manual_breakdown
        r["question_comments"] = comment_breakdown
        r["total_score"] = total
        r["percent"] = round(pct, 1)
        r["grade"] = grade_for_percent(pct)
        r["status"] = "bewertet"
        r["graded_by"] = u["id"]
        r["graded_by_name"] = u["username"]
        r["graded_at"] = datetime.now(timezone.utc).isoformat()
        r["comment"] = comment
        r["released"] = True
        return {
            "ok": True, "user_id": r["user_id"], "test_id": r["test_id"],
            "test_title": r["test_title"], "percent": r["percent"], "grade": r["grade"],
        }

    out = update_db("results", mutate)
    if "error" in out:
        return jsonify(out), 400
    log_event("test_graded", grader_id=u["id"], result_id=result_id)

    bot_send_dm(
        out["user_id"],
        "Deine Prüfung wurde bewertet",
        [f"**{out['test_title']}**\nNote {out['grade']} ({out['percent']}%)"],
        link_url=PUBLIC_URL,
        link_label="Ergebnis ansehen",
    )

    return jsonify(out)


# ----- Tests & Fragen verwalten (Fortbildungsleitung) -----

@app.route("/api/admin/tests", methods=["GET", "POST"])
@require_role("fortbildungsleitung")
def api_admin_tests():
    if request.method == "GET":
        return jsonify(load_db("tests"))
    body = request.get_json(force=True, silent=True) or {}
    test_id = body.get("id") or f"test_{int(datetime.now(timezone.utc).timestamp())}"
    new_test = {
        "id": test_id, "title": body.get("title", "Neue Fortbildung"),
        "time_limit_minutes": int(body.get("time_limit_minutes", 30)),
        "max_points": int(body.get("max_points", 0)),
        "content": body.get("content", []),
        "question_order": body.get("question_order", []),
        "prerequisite_test_id": body.get("prerequisite_test_id") or None,
    }

    def mutate(tests):
        tests[test_id] = new_test

    update_db("tests", mutate)
    return jsonify(new_test)


@app.route("/api/admin/tests/<test_id>", methods=["PUT", "DELETE"])
@require_role("fortbildungsleitung")
def api_admin_test_detail(test_id):
    if request.method == "DELETE":
        def mutate(tests):
            tests.pop(test_id, None)
        update_db("tests", mutate)
        return jsonify({"ok": True})

    body = request.get_json(force=True, silent=True) or {}

    def mutate(tests):
        t = tests.get(test_id)
        if not t:
            return {"error": "not_found"}
        t["title"] = body.get("title", t["title"])
        t["time_limit_minutes"] = int(body.get("time_limit_minutes", t["time_limit_minutes"]))
        t["max_points"] = int(body.get("max_points", t["max_points"]))
        if "content" in body:
            t["content"] = body["content"]
        if "question_order" in body:
            t["question_order"] = body["question_order"]
        if "prerequisite_test_id" in body:
            t["prerequisite_test_id"] = body.get("prerequisite_test_id") or None
        return {"ok": True}

    out = update_db("tests", mutate)
    if "error" in out:
        return jsonify(out), 404
    return jsonify(out)


@app.route("/api/admin/questions", methods=["GET", "POST"])
@require_role("fortbildungsleitung")
def api_admin_questions():
    if request.method == "GET":
        return jsonify(load_db("questions"))
    body = request.get_json(force=True, silent=True) or {}
    qid = body.get("id") or f"q_{int(datetime.now(timezone.utc).timestamp()*1000)}"
    q = {
        "id": qid, "test_id": body["test_id"], "type": body["type"],
        "points": float(body.get("points", 1)), "text": body.get("text", ""),
    }
    if q["type"] == "mc":
        q["options"] = body.get("options", [])
        q["correct"] = body.get("correct", [])
    elif q["type"] == "tf":
        q["correct"] = bool(body.get("correct", True))

    def mutate_q(questions):
        questions[qid] = q

    update_db("questions", mutate_q)

    def mutate_t(tests):
        t = tests.get(q["test_id"])
        if t and qid not in t["question_order"]:
            t["question_order"].append(qid)

    update_db("tests", mutate_t)
    return jsonify(q)


@app.route("/api/admin/questions/<qid>", methods=["PUT", "DELETE"])
@require_role("fortbildungsleitung")
def api_admin_question_detail(qid):
    if request.method == "DELETE":
        def mutate_q(questions):
            questions.pop(qid, None)
        update_db("questions", mutate_q)

        def mutate_t(tests):
            for t in tests.values():
                if qid in t["question_order"]:
                    t["question_order"].remove(qid)
        update_db("tests", mutate_t)
        return jsonify({"ok": True})

    body = request.get_json(force=True, silent=True) or {}

    def mutate(questions):
        q = questions.get(qid)
        if not q:
            return {"error": "not_found"}
        q["text"] = body.get("text", q["text"])
        q["points"] = float(body.get("points", q["points"]))
        if q["type"] == "mc":
            q["options"] = body.get("options", q.get("options", []))
            q["correct"] = body.get("correct", q.get("correct", []))
        elif q["type"] == "tf":
            q["correct"] = bool(body.get("correct", q.get("correct", True)))
        return {"ok": True}

    out = update_db("questions", mutate)
    if "error" in out:
        return jsonify(out), 404
    return jsonify(out)


@app.route("/api/admin/results")
@require_role("fortbildungsleitung")
def api_admin_results():
    results = load_db("results")
    all_r = [r for r in results.values() if r["status"] not in ("in_bearbeitung", "bereit")]
    all_r.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    return jsonify(all_r)


@app.route("/api/admin/stats")
@require_role("fortbildungsleitung")
def api_admin_stats():
    results = load_db("results")
    tests = load_db("tests")
    graded = [r for r in results.values() if r["status"] == "bewertet"]

    by_test = {}
    for r in graded:
        by_test.setdefault(r["test_id"], []).append(r)

    stats = []
    for test_id, rs in by_test.items():
        percents = [r["percent"] for r in rs if r.get("percent") is not None]
        passed = sum(1 for p in percents if p >= PASS_PERCENT)
        total = len(percents)
        stats.append({
            "test_id": test_id,
            "test_title": tests.get(test_id, {}).get("title", test_id),
            "attempts": total,
            "avg_percent": round(sum(percents) / total, 1) if total else None,
            "pass_rate": round(passed / total * 100, 1) if total else None,
            "fail_rate": round((total - passed) / total * 100, 1) if total else None,
        })
    stats.sort(key=lambda s: s["test_title"])
    return jsonify(stats)


@app.route("/api/admin/settings")
@require_role("fortbildungsleitung")
def api_admin_settings():
    return jsonify(load_db("settings"))


# ---------------------------------------------------------------------------
# Discord-Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

OWNER_USER_ID = 1211683189186105434


# ----- Components V2 Hilfsfunktionen (kein Embed, keine Emojis, farblose Container) -----

def layout_message(title, *blocks, file_ref=None):
    """Baut eine Components-V2-Nachricht: optional eine Datei-Vorschau ganz oben,
    dann '# Titel' + Separator + weitere Textblöcke, jeweils durch einen Separator
    getrennt. file_ref ist der Dateiname eines im selben discord.File-Anhang
    mitgeschickten Attachments (Components V2 zeigt Anhänge sonst nicht an)."""
    view = discord.ui.LayoutView()
    items = []
    if file_ref:
        items.append(discord.ui.File(f"attachment://{file_ref}"))
        items.append(discord.ui.Separator())
    items.append(discord.ui.TextDisplay(f"# {title}"))
    items.append(discord.ui.Separator())
    for i, block in enumerate(blocks):
        if i > 0:
            items.append(discord.ui.Separator())
        items.append(discord.ui.TextDisplay(block))
    container = discord.ui.Container(*items)
    view.add_item(container)
    return view


async def send_layout(interaction, title, *blocks, followup=False):
    view = layout_message(title, *blocks)
    if followup:
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


# ----- Backup -----

async def do_backup(reason="manuell"):
    settings = load_db("settings")
    channel_id = settings.get("backup_channel_id")
    if not channel_id:
        return False, "Kein Backup-Kanal konfiguriert. Bitte zuerst /setup ausführen."
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    except discord.HTTPException:
        return False, "Backup-Kanal nicht erreichbar."

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with DB_LOCK:
            for name in DB_FILES:
                fp = db_path(name)
                if fp.exists():
                    zf.write(fp, arcname=f"database/{name}.json")
    buf.seek(0)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{ts}.zip"
    stamp = datetime.now(timezone.utc).strftime("%A, %d. %B %Y um %H:%M UTC")
    is_auto = "automatisch" in reason
    title = "Automatisches Datenbank-Backup" if is_auto else "Manuelles Datenbank-Backup"

    blocks = [
        "Die Datenbank wurde als ZIP-Archiv gesichert.",
        f"**Zeitpunkt:** {stamp}\n**Datei:** `{filename}`",
        "Nächstes Backup in 24 Stunden" if is_auto else "Das nächste automatische Backup folgt in 24 Stunden.",
    ]
    view = layout_message(title, *blocks, file_ref=filename)
    await channel.send(view=view, file=discord.File(buf, filename=filename))
    log_event("backup", reason=reason, filename=filename)
    return True, filename


@tasks.loop(hours=24)
async def auto_backup_loop():
    await do_backup(reason="automatisch (24h)")


@bot.event
async def on_ready():
    print(f"Eingeloggt als {bot.user} ({bot.user.id})")
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"Slash-Command-Sync fehlgeschlagen: {e}")
    if not auto_backup_loop.is_running():
        auto_backup_loop.start()


def is_owner():
    """Alle Befehle sind ausschliesslich für den konfigurierten Discord-User nutzbar."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_USER_ID
    return app_commands.check(predicate)


class SetupView(discord.ui.LayoutView):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.guild = guild
        settings = load_db("settings")
        self.picked = {
            "fortbildungsleitung_role_id": settings.get("fortbildungsleitung_role_id") or None,
            "fortbilder_role_id": settings.get("fortbilder_role_id") or None,
            "mitarbeiter_role_id": settings.get("mitarbeiter_role_id") or None,
            "backup_channel_id": settings.get("backup_channel_id") or None,
            "review_channel_id": settings.get("review_channel_id") or None,
        }

    # Hinweis zur Struktur: Discord/discord.py serialisiert Auswahlmenüs und Buttons,
    # die per @row.select()/@row.button() an eine ActionRow gebunden sind, aktuell nur
    # dann korrekt, wenn diese ActionRow eine EIGENE, oberste Komponente der LayoutView
    # ist. Werden dieselben ActionRows stattdessen als Kind-Elemente in einen Container
    # gepackt, verliert die Instanz beim Rendern ihre Inhalte (leeres components-Array,
    # von Discord mit einem Validierungsfehler abgelehnt). Der Text/Titel-Block bleibt
    # daher in einem Container (rein statisch, keine Interaktion nötig), alle
    # Auswahlmenüs und der Speichern-Button folgen direkt darunter als eigene Zeilen.
    # TODO: sobald Discord/discord.py Auswahlmenüs innerhalb eines Container zuverlässig
    # unterstützt, row_leitung bis row_save wieder als Kind-Elemente in `header` packen,
    # damit alles optisch in einem einzigen Container liegt.
    header = discord.ui.Container(
        discord.ui.TextDisplay("# HHB Fortbildungszentrum – Einrichtung"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            "Wähle für jede Rolle bzw. jeden Kanal die passende Option aus und "
            "klicke danach auf **Speichern**. Der Bewertungs-Kanal ist optional."
        ),
    )

    row_leitung = discord.ui.ActionRow()

    @row_leitung.select(cls=discord.ui.RoleSelect, placeholder="Rolle: Fortbildungsleitung")
    async def select_leitung(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.picked["fortbildungsleitung_role_id"] = str(select.values[0].id)
        await interaction.response.defer()

    row_fortbilder = discord.ui.ActionRow()

    @row_fortbilder.select(cls=discord.ui.RoleSelect, placeholder="Rolle: Fortbilder")
    async def select_fortbilder(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.picked["fortbilder_role_id"] = str(select.values[0].id)
        await interaction.response.defer()

    row_mitarbeiter = discord.ui.ActionRow()

    @row_mitarbeiter.select(cls=discord.ui.RoleSelect, placeholder="Rolle: Mitarbeiter")
    async def select_mitarbeiter(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.picked["mitarbeiter_role_id"] = str(select.values[0].id)
        await interaction.response.defer()

    row_backup = discord.ui.ActionRow()

    @row_backup.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                        placeholder="Kanal: Backup")
    async def select_backup(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.picked["backup_channel_id"] = str(select.values[0].id)
        await interaction.response.defer()

    row_review = discord.ui.ActionRow()

    @row_review.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                        placeholder="Kanal: Bewertung (optional)")
    async def select_review(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.picked["review_channel_id"] = str(select.values[0].id)
        await interaction.response.defer()

    row_save = discord.ui.ActionRow()

    @row_save.button(label="Speichern", style=discord.ButtonStyle.primary)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        required = ("fortbildungsleitung_role_id", "fortbilder_role_id", "mitarbeiter_role_id", "backup_channel_id")
        if any(not self.picked.get(k) for k in required):
            await interaction.response.send_message(
                view=layout_message("Fehlende Auswahl", "Bitte wähle zuerst alle Pflichtfelder aus (alles außer dem Bewertungs-Kanal)."),
                ephemeral=True,
            )
            return

        def mutate(settings):
            settings["guild_id"] = str(self.guild.id)
            settings["fortbildungsleitung_role_id"] = self.picked["fortbildungsleitung_role_id"]
            settings["fortbilder_role_id"] = self.picked["fortbilder_role_id"]
            settings["mitarbeiter_role_id"] = self.picked["mitarbeiter_role_id"]
            settings["backup_channel_id"] = self.picked["backup_channel_id"]
            settings["review_channel_id"] = self.picked.get("review_channel_id") or ""

        update_db("settings", mutate)

        review = self.picked.get("review_channel_id")
        await interaction.response.edit_message(
            view=layout_message(
                "Einstellungen gespeichert",
                f"**Fortbildungsleitung:** <@&{self.picked['fortbildungsleitung_role_id']}>\n"
                f"**Fortbilder:** <@&{self.picked['fortbilder_role_id']}>\n"
                f"**Mitarbeiter:** <@&{self.picked['mitarbeiter_role_id']}>\n"
                f"**Backup-Kanal:** <#{self.picked['backup_channel_id']}>\n"
                f"**Bewertungs-Kanal:** " + (f"<#{review}>" if review else "nicht gesetzt"),
            ),
        )
        self.stop()


@bot.tree.command(name="setup", description="Öffnet die interaktive Einrichtung für Rollen und Kanäle")
@is_owner()
async def setup_cmd(interaction: discord.Interaction):
    view = SetupView(interaction.guild)
    await interaction.response.send_message(view=view, ephemeral=True)


@bot.tree.command(name="backup", description="Erstellt sofort ein Backup der Datenbank")
@is_owner()
async def backup_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await do_backup(reason="manuell")
    if ok:
        await send_layout(interaction, "Backup erstellt", f"**Datei:** `{msg}`", followup=True)
    else:
        await send_layout(interaction, "Fehler", msg, followup=True)


@bot.tree.command(name="reload", description="Stellt die Datenbank aus dem letzten Backup wieder her")
@is_owner()
async def reload_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    settings = load_db("settings")
    channel_id = settings.get("backup_channel_id")
    if not channel_id:
        await send_layout(interaction, "Fehler", "Kein Backup-Kanal konfiguriert.", followup=True)
        return
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    except discord.HTTPException:
        await send_layout(interaction, "Fehler", "Backup-Kanal nicht erreichbar.", followup=True)
        return

    found = None
    async for msg in channel.history(limit=100):
        if msg.author.id == bot.user.id and msg.attachments:
            for att in msg.attachments:
                if att.filename.endswith(".zip"):
                    found = att
                    break
        if found:
            break

    if not found:
        await send_layout(interaction, "Fehler", "Kein Backup gefunden.", followup=True)
        return

    data = await found.read()
    try:
        with DB_LOCK:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    if name.startswith("database/") and name.endswith(".json"):
                        target = BASE_DIR / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, open(target, "wb") as dst:
                            dst.write(src.read())
    except zipfile.BadZipFile:
        await send_layout(interaction, "Fehler", "Backup-Datei ist beschädigt.", followup=True)
        return

    log_event("reload", by=str(interaction.user.id), filename=found.filename)
    await send_layout(interaction, "Datenbank wiederhergestellt", f"**Datei:** `{found.filename}`", followup=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        title, body = "Keine Berechtigung", f"Dieser Befehl kann nur von <@{OWNER_USER_ID}> ausgeführt werden."
    else:
        title, body = "Fehler", f"Es ist ein Fehler aufgetreten: {error}"
    view = layout_message(title, body)
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def run_flask():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(DISCORD_BOT_TOKEN)

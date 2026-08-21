#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BYA ONE — la macchina del secondo prodotto (paper test).

Meccanica come DECISA il 16/08/2026 (sentenza-vault-unico) e regole di
rollover dettate il 17/08:
- token unico perpetuo; ogni sottoscrizione spende SUBITO in Bitcoin lo
  sconto equivalente a uno zero a 3 anni (tasso FRED THREEFY3 del giorno);
- la gamba titoli detiene T-bill a 3 mesi ROLLATI (tasso FRED DTB3),
  fusione a valore conservato (stessa cura della produzione);
- fee NEL MOTORE: 0,10% gestione/anno (accrual giornaliero) + 14%
  performance con high-water mark al 31/12 — prelevate SOLO dalla gamba
  titoli, mai dal Bitcoin;
- uscite ordinarie: 2% delle quote il primo giorno utile del mese,
  USCITA IN NATURA (stable dalla gamba titoli + sats), pro-rata esatto;
- ordine del giorno: rollover -> fee -> mark -> sottoscrizione -> uscita;
- briefing Telegram [ONE] e sigillo on-chain proprio (tag BYAONE1) sul
  medesimo Notaio Sepolia della produzione.

RIUSA blackbox.py (stesso repo): prezzo BTC, Telegram, RPC. Registro e
giornali SEPARATI: data/registro_one.json, operazioni_one.csv,
sigilli_one.csv. Non tocca nulla della produzione.
"""

import csv
import io
import json
import os
import random
from datetime import datetime, timezone, date, timedelta

import blackbox as BB   # moduli condivisi collaudati (prezzi, telegram, rpc)

# ---------------- parametri del prodotto ----------------
TETTO_ANNI = 3                 # sconto speso all'ingresso: zero a 3 anni
TBILL_GIORNI = 91              # gamba titoli: T-bill 3 mesi rollati
FEE_GESTIONE = 0.0010          # 0,10% l'anno, accrual giornaliero
FEE_PERFORMANCE = 0.14         # 14% con HWM al 31/12
USCITA_MENSILE = 0.02          # 2% delle quote, in natura
SEME_INIZIALE = 10_000.0       # prima esecuzione: fondo seminato a quota 1
# cadenza sottoscrizioni: identica alla produzione (se definita la',
# vincono i suoi valori)
PROB_PICCO = getattr(BB, "PROB_PICCO", 0.06)
PROB_INGRESSO = getattr(BB, "PROB_INGRESSO", 0.32)
PICCO_MIN = getattr(BB, "PICCO_MIN", 2_000)
PICCO_MAX = getattr(BB, "PICCO_MAX", 15_000)
INGRESSO_MIN = getattr(BB, "INGRESSO_MIN", 200)
INGRESSO_MAX = getattr(BB, "INGRESSO_MAX", 3_000)

REGISTRO_FILE = os.path.join("data", "registro_one.json")
OPERAZIONI_FILE = os.path.join("data", "operazioni_one.csv")
SIGILLI_FILE = os.path.join("data", "sigilli_one.csv")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"


# ---------------- fonti ufficiali ----------------
def tasso_fred(sid):
    """Ultimo valore disponibile della serie FRED (decimale)."""
    import requests
    r = requests.get(FRED_URL.format(sid=sid), timeout=60)
    r.raise_for_status()
    ultimo = None
    for riga in csv.reader(io.StringIO(r.text)):
        if len(riga) >= 2 and riga[1].strip() not in ("", "."):
            try:
                ultimo = float(riga[1]) / 100.0
            except ValueError:
                continue
    if ultimo is None:
        raise RuntimeError("serie FRED %s vuota" % sid)
    return ultimo


# ---------------- registro ----------------
def carica_registro():
    if os.path.exists(REGISTRO_FILE):
        with open(REGISTRO_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def salva_registro(reg):
    os.makedirs("data", exist_ok=True)
    with open(REGISTRO_FILE, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=1, ensure_ascii=False)


def scrivi_operazione(ts, tipo, importo, quota, quote, prezzo_btc, btc_qty, nota):
    os.makedirs("data", exist_ok=True)
    nuovo = not os.path.exists(OPERAZIONI_FILE)
    with open(OPERAZIONI_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuovo:
            w.writerow(["timestamp", "tipo", "importo_usdc", "quota",
                        "quote", "btc_usd", "btc_qty", "nota"])
        w.writerow([ts, tipo, round(importo, 2), round(quota, 6),
                    round(quote, 4), round(prezzo_btc, 2),
                    round(btc_qty, 8), nota])


# ---------------- meccanica ----------------
def valore_titoli(reg, oggi):
    """Valore maturato della gamba titoli (accrual lineare del T-bill)."""
    t = reg["titoli"]
    giorni = max((oggi - date.fromisoformat(t["data_ingresso"])).days, 0)
    return t["principal_usdc"] * (1 + t["apy"] * giorni / 365.0)


def sconto_btc(importo, y3):
    """Spesa piena dello sconto: la parte che va SUBITO in Bitcoin."""
    return importo * (1 - 1 / (1 + y3) ** TETTO_ANNI)


def accorpa_titoli(reg, importo_nuovo, y3m, oggi):
    """Fusione a valore conservato: il maturato di oggi diventa il nuovo
    principal, il capitale nuovo si somma, tasso = T-bill del giorno."""
    maturato = valore_titoli(reg, oggi)
    reg["titoli"] = {"principal_usdc": round(maturato + importo_nuovo, 2),
                     "apy": y3m,
                     "data_ingresso": oggi.isoformat(),
                     "scadenza": reg["titoli"]["scadenza"]}


def ciclo(reg, y3, y3m, prezzo_btc, adesso):
    oggi = adesso.date()
    ts = adesso.isoformat(timespec="seconds")
    eventi = []
    if reg.get("ultimo_ciclo") == oggi.isoformat():
        return reg, eventi, False    # idempotente: un ciclo al giorno

    # --- 0) performance fee al cambio d'anno (sulla quota del 31/12) ---
    anno_chiuso = reg.get("anno_quota", {}).get("anno")
    if anno_chiuso and anno_chiuso < oggi.year:
        q_fine = reg["anno_quota"]["quota"]
        hwm = reg.get("hwm", 1.0)
        if q_fine > hwm and reg["quote_totali"] > 0:
            prelievo = (q_fine - hwm) * FEE_PERFORMANCE * reg["quote_totali"]
            mat = valore_titoli(reg, oggi)
            prelievo = min(prelievo, mat * 0.5)
            reg["titoli"]["principal_usdc"] = round(
                (mat - prelievo) / (1 + reg["titoli"]["apy"] *
                 max((oggi - date.fromisoformat(reg["titoli"]["data_ingresso"])).days, 0) / 365.0), 2)
            reg["hwm"] = q_fine
            eventi.append(f"Performance fee {anno_chiuso}: {prelievo:,.2f} USDC "
                          f"(14% oltre HWM {hwm:.4f} -> nuovo HWM {q_fine:.4f})")
            scrivi_operazione(ts, "FEE_PERFORMANCE", prelievo, q_fine,
                              reg["quote_totali"], prezzo_btc,
                              reg["btc_qty"], f"anno {anno_chiuso}")
        else:
            eventi.append(f"Performance fee {anno_chiuso}: nessun prelievo "
                          f"(quota {q_fine:.4f} <= HWM {reg.get('hwm', 1.0):.4f})")
        reg["anno_quota"] = {}

    # --- 1) rollover della gamba titoli (T-bill scaduto) ---
    if oggi >= date.fromisoformat(reg["titoli"]["scadenza"]):
        maturato = valore_titoli(reg, oggi)
        reg["titoli"] = {"principal_usdc": round(maturato, 2), "apy": y3m,
                         "data_ingresso": oggi.isoformat(),
                         "scadenza": (oggi + timedelta(days=TBILL_GIORNI)).isoformat()}
        eventi.append(f"Rollover T-bill: {maturato:,.2f} USDC @ {y3m*100:.2f}% "
                      f"(scad. {reg['titoli']['scadenza']})")
        scrivi_operazione(ts, "ROLLOVER_TBILL", maturato, 0, 0,
                          prezzo_btc, reg["btc_qty"], f"nuovo tasso {y3m*100:.2f}%")

    # --- 2) management fee (accrual del giorno, dalla gamba titoli) ---
    mat = valore_titoli(reg, oggi)
    fee_g = mat * FEE_GESTIONE / 365.0
    if fee_g > 0:
        gg = max((oggi - date.fromisoformat(reg["titoli"]["data_ingresso"])).days, 0)
        reg["titoli"]["principal_usdc"] = round(
            (mat - fee_g) / (1 + reg["titoli"]["apy"] * gg / 365.0), 2)
        reg["fee_gestione_cum"] = round(reg.get("fee_gestione_cum", 0.0) + fee_g, 4)

    # --- 3) mark ---
    def quota_ora():
        return (valore_titoli(reg, oggi) + reg["btc_qty"] * prezzo_btc) / reg["quote_totali"]

    # --- 4) sottoscrizione simulata (cadenza della produzione) ---
    r = random.random()
    if r < PROB_PICCO:
        importo = round(random.uniform(PICCO_MIN, PICCO_MAX), -1)
    elif r < PROB_PICCO + PROB_INGRESSO:
        importo = round(random.uniform(INGRESSO_MIN, INGRESSO_MAX), -1)
    else:
        importo = 0.0
    if importo > 0:
        q = quota_ora()
        quote_nuove = importo / q
        in_btc = sconto_btc(importo, y3)
        reg["btc_qty"] += in_btc / prezzo_btc
        accorpa_titoli(reg, importo - in_btc, y3m, oggi)
        reg["quote_totali"] += quote_nuove
        eventi.append(f"Sottoscrizione: {importo:,.0f} USDC @ quota {q:.4f} -> "
                      f"{quote_nuove:,.2f} quote | sconto a 3a ({y3*100:.2f}%): "
                      f"{in_btc:,.2f} USDC -> {in_btc/prezzo_btc:.8f} BTC SUBITO")
        scrivi_operazione(ts, "SOTTOSCRIZIONE", importo, q, quote_nuove,
                          prezzo_btc, reg["btc_qty"],
                          f"spesa piena sconto {in_btc:.2f}")
    else:
        eventi.append("Nessun ingresso oggi")

    # --- 5) uscita ordinaria mensile, IN NATURA ---
    mese = oggi.strftime("%Y-%m")
    if reg.get("ultima_uscita_mese") != mese and oggi.day <= 3 \
            and reg.get("ultima_uscita_mese") is not None:
        reg["ultima_uscita_mese"] = mese
        q = quota_ora()
        quote_out = reg["quote_totali"] * USCITA_MENSILE
        fraz = quote_out / reg["quote_totali"]
        stable_out = valore_titoli(reg, oggi) * fraz
        sats_out = reg["btc_qty"] * fraz
        gg = max((oggi - date.fromisoformat(reg["titoli"]["data_ingresso"])).days, 0)
        reg["titoli"]["principal_usdc"] = round(
            reg["titoli"]["principal_usdc"] * (1 - fraz), 2)
        reg["btc_qty"] *= (1 - fraz)
        reg["quote_totali"] -= quote_out
        eventi.append(f"USCITA IN NATURA (2% mensile): {quote_out:,.2f} quote @ "
                      f"{q:.4f} = {stable_out:,.2f} USDC + "
                      f"{sats_out:.8f} BTC consegnati")
        scrivi_operazione(ts, "USCITA_IN_NATURA", stable_out + sats_out * prezzo_btc,
                          q, -quote_out, prezzo_btc, reg["btc_qty"],
                          f"stable {stable_out:.2f} + BTC {sats_out:.8f}")
    elif reg.get("ultima_uscita_mese") is None:
        reg["ultima_uscita_mese"] = mese     # il primo mese non esce nessuno

    # --- 6) memoria della quota per l'HWM di fine anno ---
    reg["anno_quota"] = {"anno": oggi.year, "quota": round(quota_ora(), 6)}
    reg["ultimo_ciclo"] = oggi.isoformat()
    return reg, eventi, True


# ---------------- sigillo (proprio, stesso Notaio) ----------------
def sigillo_one(adesso):
    chiave = os.environ.get("SEPOLIA_PRIVATE_KEY")
    if not chiave:
        return None, "SEPOLIA_PRIVATE_KEY assente: sigillo saltato"
    try:
        from eth_account import Account
    except ImportError:
        return None, "eth-account non installata: sigillo saltato"
    import hashlib
    h = hashlib.sha256()
    for p in (REGISTRO_FILE, OPERAZIONI_FILE, SIGILLI_FILE):
        if os.path.exists(p):
            with open(p, "rb") as f:
                h.update(f.read())
    digest = h.hexdigest()
    acct = Account.from_key(chiave)
    payload = ("BYAONE1:" + digest).encode()
    ultimo_err = "nessun RPC raggiungibile"
    for rpc in BB.SEPOLIA_RPCS:
        try:
            nonce = int(BB.http_post_json(rpc, {"jsonrpc": "2.0", "id": 1,
                        "method": "eth_getTransactionCount",
                        "params": [acct.address, "pending"]})["result"], 16)
            gas = int(BB.http_post_json(rpc, {"jsonrpc": "2.0", "id": 2,
                      "method": "eth_gasPrice", "params": []})["result"], 16)
            tx = {"nonce": nonce, "to": acct.address, "value": 0,
                  "gas": 60000, "gasPrice": int(gas * 1.3),
                  "data": "0x" + payload.hex(), "chainId": 11155111}
            firmata = Account.sign_transaction(tx, chiave)
            raw = getattr(firmata, "raw_transaction", None) or firmata.rawTransaction
            raw_hex = raw.hex()
            if not raw_hex.startswith("0x"):
                raw_hex = "0x" + raw_hex
            r = BB.http_post_json(rpc, {"jsonrpc": "2.0", "id": 3,
                                        "method": "eth_sendRawTransaction",
                                        "params": [raw_hex]})
            if "result" in r:
                nuovo = not os.path.exists(SIGILLI_FILE)
                with open(SIGILLI_FILE, "a", encoding="utf-8") as f:
                    if nuovo:
                        f.write("data,hash_sha256,tx_hash\n")
                    f.write(f"{adesso.isoformat(timespec='seconds')},{digest},{r['result']}\n")
                return {"hash": digest, "tx": r["result"]}, None
            ultimo_err = str(r.get("error", r))
        except Exception as e:
            ultimo_err = str(e)
    return None, f"sigillo non inviato: {ultimo_err}"


# ---------------- briefing ----------------
def componi_briefing(reg, eventi, y3, y3m, prezzo_btc, adesso, sig, sig_err):
    oggi = adesso.date()
    mat = valore_titoli(reg, oggi)
    nav = mat + reg["btc_qty"] * prezzo_btc
    quota = nav / reg["quote_totali"]
    r = [f"[ONE] Briefing BYA One - {adesso.isoformat(timespec='minutes')}",
         "Paper test del secondo prodotto: token unico, spesa piena dello "
         "sconto (3 anni), T-bill rollati, fee nel motore, uscite in natura.",
         "",
         f"Tasso zero 3 anni (FRED): {y3*100:.2f}% | T-bill 3 mesi: {y3m*100:.2f}%",
         f"BTC: {prezzo_btc:,.0f} USD",
         f"Sconto su un ingresso di 10.000: {sconto_btc(10000, y3):,.0f} USDC in BTC subito",
         "",
         "--- FONDO ONE ---",
         f"NAV: {nav:,.2f} USDC | quota (netta di fee): {quota:.4f} | "
         f"quote: {reg['quote_totali']:,.0f}",
         f"Titoli (T-bill @ {reg['titoli']['apy']*100:.2f}%, scad. "
         f"{reg['titoli']['scadenza']}): {mat:,.2f}",
         f"BTC: {reg['btc_qty']:.8f} ({reg['btc_qty']*prezzo_btc:,.0f} USDC) | "
         f"peso {reg['btc_qty']*prezzo_btc/nav*100:.1f}%",
         f"HWM: {reg.get('hwm', 1.0):.4f} | fee gestione cumulate: "
         f"{reg.get('fee_gestione_cum', 0.0):,.2f}",
         "",
         "--- OPERAZIONI DEL GIORNO ---"]
    r += [f"- {e}" for e in eventi]
    if sig:
        r.append(f"Sigillo on-chain: sepolia.etherscan.io/tx/{sig['tx']}")
    elif sig_err:
        r.append(f"({sig_err})")
    return "\n".join(r)




# ---------------- pubblicazione one.json (vetrina del sito) ----------------
ONE_JSON_REPO = "Alex00975/btc-accumulator-data"
ONE_JSON_PATH = "one.json"
_TOKEN_ENV = ("ONE_JSON_TOKEN", "DATA_TOKEN", "DATA_REPO_TOKEN", "GH_PAT",
              "PUSH_TOKEN", "VETRINA_TOKEN", "PAT")


def _ultimo_sigillo():
    """(tx, data) dell'ultimo sigillo registrato, per i giorni senza sigillo nuovo."""
    try:
        with open(SIGILLI_FILE, encoding="utf-8") as f:
            righe = [r.strip() for r in f if r.strip()]
        if len(righe) >= 2:
            campi = righe[-1].split(",")
            return campi[2], campi[0][:10]
    except Exception:
        pass
    return None, None


def pubblica_one_json(reg, y3, prezzo_btc, adesso, sig):
    """Scrive one.json secondo la SPEC della pagina (bya.finance, vista One) e,
    se un token e' disponibile, lo pubblica nel repo dati via API GitHub.
    In ogni caso il file resta scritto in locale (il workflow puo' committarlo)."""
    oggi = adesso.date()
    mat = valore_titoli(reg, oggi)
    nav = mat + reg["btc_qty"] * prezzo_btc
    quota = nav / reg["quote_totali"] if reg["quote_totali"] else 1.0
    sconto_pct = (1.0 - (1.0 + y3) ** -TETTO_ANNI) * 100.0
    if sig:
        tx, data_sig = sig["tx"], oggi.isoformat()
    else:
        tx, data_sig = _ultimo_sigillo()
    out = {"date": oggi.isoformat(),
           "y3": round(y3 * 100.0, 2),
           "discount": round(sconto_pct, 1),
           "live": {"quota": round(quota, 4),
                    "pct": round((quota - 1.0) * 100.0, 2),
                    "btc": round(reg["btc_qty"], 8),
                    "nav": round(nav, 2)}}
    if tx:
        out["live"]["sigillo"] = {"tx": tx, "data": data_sig or oggi.isoformat()}
    corpo = json.dumps(out)
    with open(ONE_JSON_PATH, "w", encoding="utf-8") as f:
        f.write(corpo)

    token = next((os.environ[n] for n in _TOKEN_ENV if os.environ.get(n)), None)
    if not token:
        return "one.json scritto in locale (nessun token: commit lasciato al workflow)"
    try:
        import base64
        import requests
        api = f"https://api.github.com/repos/{ONE_JSON_REPO}/contents/{ONE_JSON_PATH}"
        testa = {"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"}
        sha = None
        r = requests.get(api, headers=testa, timeout=30)
        if r.status_code == 200:
            sha = r.json().get("sha")
        dati = {"message": f"one.json {oggi.isoformat()}",
                "content": base64.b64encode(corpo.encode()).decode(),
                "branch": "main"}
        if sha:
            dati["sha"] = sha
        r = requests.put(api, headers=testa, json=dati, timeout=30)
        if r.status_code in (200, 201):
            return f"one.json pubblicato su {ONE_JSON_REPO}"
        return f"one.json: pubblicazione fallita ({r.status_code} {r.text[:120]})"
    except Exception as e:
        return f"one.json: pubblicazione fallita ({e})"


def main():
    adesso = datetime.now(timezone.utc)
    y3 = tasso_fred("THREEFY3")
    y3m = tasso_fred("DTB3")
    btc = BB.btc_spot()
    prezzo_btc = btc["prezzo_usd"]

    reg = carica_registro()
    if reg is None:
        oggi = adesso.date()
        in_btc = sconto_btc(SEME_INIZIALE, y3)
        reg = {"quote_totali": SEME_INIZIALE,
               "titoli": {"principal_usdc": round(SEME_INIZIALE - in_btc, 2),
                          "apy": y3m, "data_ingresso": oggi.isoformat(),
                          "scadenza": (oggi + timedelta(days=TBILL_GIORNI)).isoformat()},
               "btc_qty": in_btc / prezzo_btc,
               "hwm": 1.0, "anno_quota": {}, "ultima_uscita_mese": None,
               "fee_gestione_cum": 0.0, "ultimo_ciclo": None}
        scrivi_operazione(adesso.isoformat(timespec="seconds"), "SEME",
                          SEME_INIZIALE, 1.0, SEME_INIZIALE, prezzo_btc,
                          reg["btc_qty"], f"sconto {in_btc:.2f} -> BTC subito")
        eventi = [f"FONDO SEMINATO: {SEME_INIZIALE:,.0f} USDC a quota 1,0000 | "
                  f"sconto 3a {in_btc:,.2f} USDC -> {reg['btc_qty']:.8f} BTC"]
        eseguito = True
        reg["ultimo_ciclo"] = oggi.isoformat()
    else:
        reg, eventi, eseguito = ciclo(reg, y3, y3m, prezzo_btc, adesso)

    salva_registro(reg)
    sig, sig_err = (None, "gia' eseguito oggi: nessun sigillo doppio")
    if eseguito:
        sig, sig_err = sigillo_one(adesso)
    esito_json = pubblica_one_json(reg, y3, prezzo_btc, adesso, sig)
    testo = componi_briefing(reg, eventi, y3, y3m, prezzo_btc, adesso, sig, sig_err)
    print(testo)
    print(esito_json)
    if eseguito:
        BB.telegram_invia(testo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

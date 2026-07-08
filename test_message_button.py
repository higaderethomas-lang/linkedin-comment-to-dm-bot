#!/usr/bin/env python3
"""
TEST À BLANC — quel bouton « Message » le bot choisirait-il ? (ZÉRO envoi)

Ouvre chaque profil avec la session LinkedIn, applique EXACTEMENT la même logique
de sélection que le bot (exclusion sidebar + matching du nom + garde-fou), et
affiche le bouton qui SERAIT cliqué — sans jamais cliquer ni taper quoi que ce soit.

Usage :
    python3 test_message_button.py                 # liste par défaut (profils ratés)
    python3 test_message_button.py <url1> <url2>   # profils précis
    HEADLESS=0 python3 test_message_button.py      # voir le navigateur (debug)
"""

import asyncio
import os
import re
import sys
import unicodedata
from urllib.parse import quote

from core.bot import _new_browser_context

# Profils ratés lors des derniers runs (modifiable)
DEFAULT_URLS = [
    "https://www.linkedin.com/in/marc-beillaud",
    "https://www.linkedin.com/in/sebastiengros-investissements",
    "https://www.linkedin.com/in/cecileraynaud",
    "https://www.linkedin.com/in/félix-rivierre-b037a7102",
    "https://www.linkedin.com/in/pierre-loic-besse",
    "https://www.linkedin.com/in/eric-de-gouttes-06a41718",
]

# Nom attendu (pour calculer les tokens), déduit du slug si absent
EXPECTED = {
    "marc-beillaud": "Marc Beillaud",
    "sebastiengros-investissements": "Sébastien Gros",
    "cecileraynaud": "Cécile Raynaud",
    "félix-rivierre-b037a7102": "Félix Rivierre",
    "pierre-loic-besse": "Pierre-Loic Besse",
    "eric-de-gouttes-06a41718": "Eric de Gouttes",
}

# JS de DIAGNOSTIC : même logique que le bot mais SANS clic. Renvoie tous les
# boutons « Message » trouvés + lequel serait choisi.
DIAG_JS = r"""(tokens) => {
    const strip = s => (s||'').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g,'');
    const nameFromLabel = lbl => { const l = strip(lbl).trim();
        return l.startsWith('message ') ? l.slice(8).trim() : ''; };
    const matchTarget = el => {
        const nm = nameFromLabel(el.getAttribute('aria-label')||'') || nameFromLabel(el.textContent||'');
        if (!nm) return null;
        return tokens.some(t => nm.includes(t));
    };
    const inExcludedZone = el =>
        !!(el.closest('.msg-overlay-conversation-bubble') || el.closest('.msg-overlay-list-bubble') ||
           el.closest('[class*="msg-overlay"]') || el.closest('[class*="typeahead"]') ||
           el.closest('.scaffold-layout__aside') || el.closest('aside') ||
           el.closest('[class*="browsemap"]') || el.closest('[class*="pymk"]') ||
           el.closest('[class*="people-also"]') || el.closest('[class*="similar"]') ||
           el.closest('[class*="aside"]'));
    const looksMsg = el => {
        const t = strip(el.textContent||'').trim();
        const aria = strip(el.getAttribute('aria-label')||'');
        return (t === 'message' || t.startsWith('message ') ||
                aria === 'message' || aria.startsWith('message '));
    };

    const h1 = document.querySelector('h1');
    const allMsg = Array.from(document.querySelectorAll('button, a[href]')).filter(looksMsg);
    const dump = allMsg.map(el => ({
        label: (el.getAttribute('aria-label') || el.textContent.trim()).slice(0,45),
        excluded: inExcludedZone(el),
        match: matchTarget(el),           // true=cible, false=autre, null=générique
        disabled: !!el.disabled,
    }));

    const candidates = Array.from(document.querySelectorAll('button, a[href]'))
        .filter(el => looksMsg(el) && !inExcludedZone(el) && !el.disabled)
        .sort((a,b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
    let chosen = candidates.find(b => matchTarget(b) === true);
    if (!chosen) chosen = candidates.find(b => matchTarget(b) === null);
    let reason = 'ok';
    if (!chosen) {
        const foreign = candidates.find(b => matchTarget(b) === false);
        chosen = null; reason = foreign ? 'foreign_only' : 'not_found';
    }
    return {
        h1: h1 ? h1.innerText.trim() : null,
        url: location.href,
        chosen: chosen ? (chosen.getAttribute('aria-label') || chosen.textContent.trim()).slice(0,45) : null,
        reason,
        all_message_buttons: dump,
    };
}"""


def tokens_for(name: str):
    def strip(s):
        s = unicodedata.normalize("NFKD", s or "")
        return "".join(c for c in s if not unicodedata.combining(c)).lower()
    toks = [re.sub(r"[^a-z]", "", strip(t)) for t in re.split(r"[\s\-]+", name or "")]
    return [t for t in toks if len(t) >= 3]


async def main():
    urls = sys.argv[1:] or DEFAULT_URLS
    headless = os.environ.get("HEADLESS", "1") != "0"

    browser, context = await _new_browser_context(None, headless=headless)
    print(f"\n{'='*70}\n  TEST À BLANC — sélection du bouton Message (AUCUN envoi)\n"
          f"  headless={headless} | {len(urls)} profil(s)\n{'='*70}")
    try:
        for url in urls:
            slug = url.rstrip("/").split("/in/")[-1].split("?")[0]
            name = EXPECTED.get(slug, slug.replace("-", " ").title())
            toks = tokens_for(name)
            # Encodage URL (gère les accents)
            base, s = url.split("/in/", 1)
            nav = base + "/in/" + quote(s, safe="/-_~.")

            page = await context.new_page()
            try:
                await page.goto(nav, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(8)
                # retry de lecture (comme le bot)
                res = None
                for _ in range(5):
                    res = await page.evaluate(DIAG_JS, toks)
                    if res and res.get("chosen"):
                        break
                    await asyncio.sleep(2)
            except Exception as e:
                print(f"\n❌ {name}\n   erreur navigation : {e}")
                continue
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

            verdict = ("✅ ENVERRAIT à " + repr(res["chosen"])) if res.get("chosen") \
                else (f"🛑 ABANDON ({res.get('reason')}) — aucun envoi")
            print(f"\n● {name}  (tokens={toks})")
            print(f"   URL finale : {res.get('url')}")
            print(f"   h1 lu      : {res.get('h1')!r}")
            print(f"   → {verdict}")
            btns = res.get("all_message_buttons") or []
            if btns:
                print(f"   Boutons « Message » vus ({len(btns)}) :")
                for b in btns:
                    tag = {True: "CIBLE", False: "AUTRE", None: "générique"}[b["match"]]
                    zone = "exclu(sidebar)" if b["excluded"] else "barre-action"
                    print(f"      - «{b['label']}»  [{tag} | {zone}]")
            else:
                print("   (aucun bouton « Message » trouvé sur la page)")
    finally:
        try:
            await browser.close()
        except Exception:
            pass
    print(f"\n{'='*70}\n  Fin du test (rien n'a été envoyé).\n{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())

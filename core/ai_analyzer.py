import json
import re
import urllib.request
from typing import Optional

# Gemini API (modèle le moins cher : gemini-2.5-flash-lite)
GEMINI_API_KEY = "METS_TA_CLE_GEMINI_ICI"
GEMINI_MODEL   = "gemini-2.5-flash-lite"
GEMINI_URL     = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

# Mots-clés détectables sans IA (vérifiés uniquement dans les réponses de Thomas)
_TRAITE_PATTERNS = [
    # Variantes "en DM"
    "bien reçu en dm", "c'est envoyé en dm", "envoyé en dm",
    "envoyé ça en dm", "reçu en dm",
    # Variantes "en message privé"
    "en message privé", "en message prive", "en mp",
    # Variantes "boîte"
    "dans votre boîte", "dans votre boite", "dans ta boîte", "dans ta boite",
    # Variantes "envoyé" sans précision (Thomas dit souvent juste "c'est envoyé :)")
    "c'est envoyé", "c est envoyé", "c'est parti",
    "je vous ai envoyé", "je t'ai envoyé", "je te l'ai envoyé",
    "vous l'ai envoyé", "vous ai envoyé",
    "viens de vous envoyer", "viens de t'envoyer",
    # Variantes "envoi"
    "je t'envoie", "je vous envoie", "je l'envoie",
]
_CONNECTE_PATTERNS = [
    "connecte-toi", "connecte toi", "envoie-moi une demande", "ajoute-moi",
    "connectez-vous", "connectons-nous", "envoyez-moi une demande",
    "dès qu'on sera connectés", "des qu'on sera connectes",
    "pour que je puisse vous envoyer", "pour que je puisse t'envoyer",
]


def _extract_thomas_replies(thread_text: str) -> str:
    """Extrait uniquement les blocs [Réponse de Thomas] du thread_text."""
    parts = thread_text.split("[Réponse de Thomas]")
    if len(parts) <= 1:
        return ""  # pas de réponse de Thomas
    thomas_blocks = []
    for part in parts[1:]:
        # S'arrête au prochain marqueur de section s'il existe
        for marker in ("[Commentaire de", "[Réponse de"):
            idx = part.find(marker)
            if idx > 0:
                part = part[:idx]
        thomas_blocks.append(part.strip())
    return " ".join(thomas_blocks).lower()


def _pre_classify(thread_text: str, current_degree: str) -> Optional[dict]:
    """Tente de classifier sans appeler l'API. None si ambigu → IA."""
    thomas_only = _extract_thomas_replies(thread_text)
    if not thomas_only:
        return None

    # Cas certain : Thomas a clairement confirmé l'envoi
    if any(p in thomas_only for p in _TRAITE_PATTERNS):
        return {"status": "traite", "action": "nothing",
                "reason": "Détecté localement : Thomas a confirmé l'envoi"}

    # "connecte-toi" SANS 1st → en attente de connexion (cas sûr)
    if any(p in thomas_only for p in _CONNECTE_PATTERNS) and current_degree != "1st":
        return {"status": "en_attente_connexion", "action": "nothing",
                "reason": "Détecté localement : en attente de connexion"}

    # "connecte-toi" + 1st → AMBIGU : Thomas a peut-être déjà envoyé le DM
    # manuellement sans laisser de commentaire de confirmation.
    # → Toujours demander à l'IA pour ces cas (elle analyse le contexte complet)
    return None


async def analyze_comment_thread(commenter_name: str, thread_text: str, current_degree: str = '1st') -> dict:
    if not GEMINI_API_KEY or "METS" in GEMINI_API_KEY:
        return {"status": "a_traiter", "reason": "Pas de clé API"}

    # Essaie d'abord sans l'IA (économise des tokens)
    result = _pre_classify(thread_text, current_degree)
    if result:
        return result

    prompt = f"""Tu analyses un thread LinkedIn entre Thomas Higadere et {commenter_name}.
Degré de connexion ACTUEL : {current_degree}

Thread complet :
---
{thread_text}
---

⚠️ RÈGLE CRITIQUE : Si TOUT message de Thomas dans le thread suggère qu'il a déjà envoyé le DM/document, classe en "traite". Sois LARGE dans cette détection — un faux positif "traite" est moins grave qu'un DM dupliqué.

Statuts possibles :

1. "traite" → Thomas a EN UN MOMENT QUELCONQUE du thread écrit quelque chose qui implique qu'il a envoyé / partagé / transmis le DM ou document. Exemples NON exhaustifs :
   - "bien reçu en DM", "c'est envoyé en DM", "envoyé en MP"
   - "c'est envoyé", "c'est parti", "envoyé :)", "envoyé !"
   - "je vous ai envoyé", "je t'ai envoyé", "viens de t'envoyer"
   - "c'est dans votre boîte", "dans ta boîte mail"
   - "voici le doc", "voilà le lien", "lien ici", URL charlie-mail/proposal
   - "tu l'as reçu ?", "bien arrivé ?", "tu as vu mon message ?"
   - Tout texte indiquant que Thomas a partagé/donné/transmis quelque chose
   ➜ Même si Thomas a AUSSI dit "connecte-toi" plus tôt, si UNE de ses réponses suivantes indique l'envoi → "traite"

2. "en_attente_connexion" → Thomas a dit "connecte-toi/connectez-vous/connectons-nous" ET degré encore 2nd/3rd, ET aucune mention d'envoi
3. "a_convertir" → Thomas a dit "connecte-toi", degré = 1st (personne connectée), AUCUN signe d'envoi de DM dans tout le thread
4. "a_traiter" → Thomas n'a posté AUCUNE réponse dans le thread

Actions :
- "traite" → "nothing"
- "a_convertir" → "send_dm_then_comment"
- "en_attente_connexion" → "nothing"
- "a_traiter" ET 1st → "send_dm_then_comment"
- "a_traiter" ET 2nd/3rd → "post_connect_comment"

Réponds UNIQUEMENT avec du JSON valide :
{{
  "status": "traite" | "a_convertir" | "en_attente_connexion" | "a_traiter",
  "action": "nothing" | "send_dm_then_comment" | "post_connect_comment",
  "reason": "1 phrase qui cite le passage du thread justifiant le statut"
}}"""

    # Appel Gemini API
    data = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 200,
            "temperature": 0.1,
            "responseMimeType": "application/json"
        }
    }).encode()

    req = urllib.request.Request(
        GEMINI_URL,
        data=data,
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
            text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Nettoyage défensif des fences markdown si jamais Gemini en met
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            return json.loads(text)
    except Exception as e:
        return {"status": "a_traiter", "action": "send_dm_then_comment", "reason": f"Erreur Gemini: {e}"}

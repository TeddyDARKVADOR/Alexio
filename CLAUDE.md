# Alexio — règles de développement

Assistant vocal, fork de **Mark LII** (FatihMakes, CC BY-NC 4.0 — usage personnel, attribution
obligatoire). Cible : **Fedora 43 · GNOME 49.9 · Wayland · i5-8265U · pas de GPU**.

Refonte en cours : passer d'un système câblé sur Gemini Live à une architecture multi-modèle
routée. Dossier d'architecture complet : voir l'artifact « Refonte Alexio ».

---

## Architecture

Deux plans distincts, jamais fusionnés en une seule fonction :

| Plan | Interface | Nature | État |
|---|---|---|---|
| **A — Conversation** | `core/voice` | session persistante, audio bidirectionnel, ms | **fait (phase 02)** |
| **B — Requête** | `core/ai` | sans état, annulable, s | **fait (phases 01 + 03)** |

**Claude n'a pas d'API voix-à-voix** : il ne peut jamais occuper le plan A.

### Plan A — `core/voice`

```python
from core import voice

session = voice.GeminiLiveSession(api_key)
await session.connect(voice.VoiceConfig(system_instruction=..., tools=..., voice="Charon"))
async for ev in session.events():
    if ev.kind == voice.EventKind.AUDIO: ...
```

Six méthodes : `connect`, `close`, `send_audio`, `send_text`, `send_media`,
`send_tool_results`, plus `events()` et `resume_handle`. Ajouter OpenAI Realtime ou une
pile locale, c'est une classe de plus. **Anthropic ne peut pas servir ce plan.**

Événements : `AUDIO`, `TRANSCRIPT_IN`, `TRANSCRIPT_OUT`, `TURN_COMPLETE`, `TOOL_CALL`,
`RESUMPTION`, `GO_AWAY`, `INTERRUPTED`. `GO_AWAY` porte `seconds_left` : le serveur
prévient avant de raccrocher (~10 min), et `main.py` reconstruit la session sur ce signal
en gardant la conversation.

### Plan B — `core/ai`

Tout appel modèle hors conversation passe par là. Une action dit ce dont elle a besoin,
jamais qui répond :

```python
from core import ai

ai.generate("Résume ceci :\n" + texte, task="summarize").text
ai.generate(prompt, media=[ai.Image.from_path(p)], task="describe_image")
ai.generate(query, grounding=True, task="web_search").stripped
ai.generate(plan, tier=ai.Tier.DEEP, task="dev_agent", timeout=180)
```

- **`tier`** — `FAST` / `STANDARD` / `DEEP`. Ce que le travail mérite, pas un nom de produit.
  Un identifiant de modèle ne doit **jamais** apparaître dans un module d'action.
- **`task`** — l'étiquette du journal. C'est elle qui rend `logs/ai_calls.jsonl` lisible
  dans six mois : la nommer précisément est obligatoire.
- **`media`** — images *et* audio ; le type MIME décide de la capacité requise.
- Erreurs typées : `ProviderUnavailable`, `QuotaExceeded`, `EmptyResponse`,
  `CapabilityUnavailable` — toutes sous `ai.AIError`.
- Le repli entre providers est automatique sur `ProviderUnavailable` / `QuotaExceeded`.
  Une `EmptyResponse` **ne** déclenche **pas** de repli : un modèle muet le sera partout.

Un provider expose `NAME`, `CAPABILITIES`, `model_for(tier, grounding)`, `generate(...)`
et, s'il a besoin d'une clé, `has_credentials()` — rien de plus. Quatre existent :
`gemini.py`, `anthropic.py`, `openai.py`, `local.py`. En ajouter un, c'est un fichier
de ce gabarit plus une ligne dans le catalogue de `registry.py`.

### Registre et routeur (phase 03)

```python
ai.generate(prompt, task="t", budget=ai.Budget.conversation())  # latence ≤ 900 ms
ai.generate(prompt, task="t", budget=ai.Budget.private())       # ne quitte pas la machine
ai.explain_routing("fast")                                       # pourquoi ce modèle
```

- `core/ai/registry.py` — le catalogue. Chaque entrée porte `declared` (doc du fournisseur,
  vérifiée le 2026-09-07) et `measured` (p50/p95/taux d'échec depuis la télémétrie).
  `latency_ms` renvoie le mesuré dès 5 appels, le déclaré avant. `load()` **enrichit**, il
  n'écrase jamais des mesures déjà attachées.
- `core/ai/router.py` — `Budget(tier, max_latency_ms, max_cost_usd, privacy, needs)`.
  Filtre par capacité / vie privée / latence / coût, écarte ce qui échoue sous 80 % de
  réussite, trie par latence puis par prix. Rien ne convient → `NoModelFits`, qui **dit
  pourquoi pour chaque candidat**. Jamais de dégradation silencieuse (R-12).
- `core/ai/tools.py` — `ToolSpec` neutre + les trois dialectes. Gemini écrit les types en
  MAJUSCULES, Anthropic veut `input_schema`, OpenAI `function.parameters`. La conversion
  est récursive : ne traiter que le premier niveau produit un document qui a l'air correct
  et casse au premier tableau d'objets.

Quatre providers : `gemini`, `anthropic`, `openai` (HTTP direct, sans SDK), `local`.
`ai.reachable_providers()` distingue « existe » de « a une clé ici ».

Consulter le catalogue :
```bash
.venv/bin/python -c "from core.ai import registry; print(registry.describe())"
```

---

## Invariants — ne pas enfreindre

**R-01** · Aucun module hors `core/ai/` et `core/voice/` n'importe un SDK de fournisseur.
**Liste d'exemptions vide depuis la phase 02.** Appliquée par `tests/test_tool_wiring.py`.

**R-02** · Un outil ne choisit jamais son modèle. Il déclare ce qu'il fait (`task`) et ce
que le travail mérite (`tier`). Aucun identifiant de modèle hors `core/ai/`.

**R-03** · La signature des actions ne change pas :
`fn(parameters, response=None, player=None, session_memory=None, speak=None) -> str`
`core/plugin_loader.py` inspecte cette signature — la casser casse tous les plugins en silence.

**R-04** · Le jeton de confirmation est émis par l'interface (`core/confirm.py`), jamais par le
modèle. Ne pas rouvrir la faille du paramètre `confirmed=yes`.

**R-05** · Réversible → exécuter tout de suite + `push_undo`. Irréversible seulement → confirmer.
Le critère est la réversibilité, pas la gravité du mot.

**R-06** · Une capacité se mesure, elle ne se déclare pas (cf. `core/audio_devices.py`).

**R-07** · Rien de bloquant sur le thread Qt ni dans la boucle asyncio.
`run_in_executor` / `asyncio.to_thread` systématiquement.

**R-08** · Aucune chaîne visible par l'utilisateur codée en dur dans une langue.

**R-09** · Sous Wayland : **portail, jamais X11**. `mss`, `pyautogui`, `pygetwindow` sont morts ici.

**R-10** · Pas de PyGObject dans le venv. D-Bus via `jeepney` / `dbus-fast` (Python pur).
AT-SPI dans un processus fils lancé avec `/usr/bin/python3`.

**R-11** · Tout appel modèle écrit une ligne JSONL : `ts, plane, provider, model, task,
tokens_in, tokens_out, latency_ms, cost_est, ok`. Échecs et hits de cache compris.

**R-12** · Un budget impossible lève une erreur ; jamais de dégradation silencieuse.

---

## Environnement

```bash
.venv/bin/python main.py            # lancer
.venv/bin/python -m pytest          # 104 tests, sans micro/écran/clé API/réseau
.venv/bin/python -m core.telemetry  # ce que les modèles ont réellement coûté
```

- **Python : `.venv` construit sur pyenv 3.11.4** (`asyncio.TaskGroup` exige ≥ 3.11).
  Le Python système est en 3.14.6 — ne pas l'utiliser pour l'app.
- `requirements.txt` = intentions · `requirements.lock` = 74 versions figées ·
  `requirements-extra.txt` = formats de fichiers optionnels · `requirements-dev.txt` = pytest.
- Pas de GPU. Tout le local est CPU/int8. 5 Go de RAM libres, zram déjà chargé.

**Les imports optionnels se gardent avec `except Exception`, jamais `except ImportError`.**
Sur Linux, `pyautogui` ouvre un display X à l'import et lève `DisplayConnectionError` quand il
n'y en a pas (SSH, TTY, unité systemd, CI). Ce n'est pas une `ImportError` : la garde étroite
laissait l'exception tuer l'import de `main.py` en entier. Cinq modules étaient concernés.

### Surface système Fedora 43 / Wayland

| Besoin | À utiliser | À ne plus utiliser |
|---|---|---|
| Capture écran | `org.freedesktop.portal.Screenshot` v2 (`interactive: false`) | `mss` — image noire |
| Clavier/souris | `org.freedesktop.portal.RemoteDesktop` v2 + libei 1.5.0 (`restore_token`) | `pyautogui` — XWayland seulement |
| Luminosité | `org.freedesktop.login1.Session.SetBrightness(ssu)` | `brightnessctl` — absent ; `gsd.Power.Screen` n'existe plus sur GNOME 49 |
| Volume | `wpctl` / `pactl` (PipeWire) | — |
| Corbeille | `org.freedesktop.portal.Trash` | `send2trash` |
| Fenêtres | AT-SPI (`at-spi2-core` 2.58.7) | `pygetwindow` — sans objet sous Wayland |
| Rappels | `systemd-run --user` (présent), `at` en secours | — |

Fonctionnent déjà : `gsettings` (mode sombre), `notify-send`, `ffmpeg`, `xdg-open`.

### Pile locale viable sur ce matériel

- **STT** : Parakeet TDT 0.6B v3 (ONNX int8, ~30× temps réel, français). *Pas* faster-whisper.
- **TTS** : Piper (OHF-Voice v1.6.0, ~40 ms au premier son, voix `siwis`/`tom`/`upmc`).
  *Pas* Kokoro : 3,6 s et 2 Go de pic.
- **VAD / réveil** : Silero VAD + openWakeWord.
- **LLM local** : Qwen3 1.7B Q4 maximum, réservé à la classification d'intention.

---

## Dette connue

Corrigé en phase 00 : le prompt qui appelait `agent_task` (inexistant), le message réseau en
turc, les cinq gardes d'import trop étroites, `google-generativeai` inutilisé dans les requirements.

Corrigé en phase 01 : les ~40 appels Gemini dispersés sur 10 modules, la lecture de
`response.text` seule qui vidait les réponses ancrées (grounding), le circuit-breaker de quota
qui devinait par correspondance de chaînes.

Corrigé en phase 02 : `go_away`/`time_left` désormais lus, les 258 lignes de session Live
morte de `screen_processor.py` supprimées, la socket Live fermée explicitement à chaque
reconnexion (l'ancien `async with` implicite avait disparu du refactor).

Corrigé en phase 03 : `registry.load()` qui écrasait les mesures au lieu de les enrichir.

Reste :

- `core/llm_client.py` est remplacé par `core/ai/local.py` — **à supprimer**.
  `core/stt.py`, `core/tts.py`, `core/installer.py` attendent Parakeet/Piper (phase 05).
- `actions/screen_processor.py` : `_capture_screen` utilise encore `mss` (phase 04).
- Le classifieur d'intention (classe « Réflexe », < 200 ms sans modèle) reste à faire —
  il vit avec le mot de réveil en phase 05.
- Dashboard : clé 6 caractères en SHA-256 + sel fixe, jetons d'appareil sans expiration,
  page `/` authentifiée côté client seulement, ouverture automatique du pare-feu avec élévation.
- `config/certs/jarvis.key` est dans l'historique Git malgré le `.gitignore`.
- 104 tests, mais aucune CI ne les lance encore.

---

## Conventions de code

- Commentaires denses expliquant le *pourquoi* d'une décision — c'est le style du dépôt, le garder.
- Alignement vertical des dicts et assignations (style existant).
- Messages d'erreur : ce qui a échoué + comment le corriger. Pas d'excuses.

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
Le verdict vit dans `core/tool_policy.py`, **une seule table**, consultée par `_execute_tool`
avant tout appel. Elle juge sur les *arguments* : `file_controller` qui lit et
`file_controller` qui supprime portent le même nom d'outil.

**R-06** · Une capacité se mesure, elle ne se déclare pas (cf. `core/audio_devices.py`).

**R-07** · Rien de bloquant sur le thread Qt ni dans la boucle asyncio.
`run_in_executor` / `asyncio.to_thread` systématiquement.

**R-08** · Aucune chaîne visible par l'utilisateur codée en dur dans une langue.

**R-09** · Sous Wayland : **portail, jamais X11**. Passer par `core/desktop`, jamais
appeler `mss` / `pyautogui` / `pygetwindow` directement depuis une action.
Appliquée par `tests/test_input_facade.py` — liste d'exemptions **vide**. La règle est
restée décorative jusqu'à la phase 08 parce que la façade n'exposait rien pour le clavier :
9 modules, 140 appels `pyautogui` directs, faute d'ailleurs où aller. Voir `core/desktop/input.py`.


**R-10** · Pas de PyGObject dans le venv. D-Bus via `jeepney` / `dbus-fast` (Python pur).
AT-SPI dans un processus fils lancé avec `/usr/bin/python3`.

**R-11** · Tout appel modèle écrit une ligne JSONL : `ts, plane, provider, model, task,
tokens_in, tokens_out, latency_ms, cost_est, ok`. Échecs et hits de cache compris.

**R-12** · Un budget impossible lève une erreur ; jamais de dégradation silencieuse.
**R-13** · **Multi-plateforme par construction.** Aucun module de `core/` n'importe un paquet
spécifique à un OS au niveau module — toujours dans la fonction qui s'en sert. C'est ce qui
permet à `desktop.report("Windows")` et aux tests d'inspecter Windows depuis Linux.
`tests/test_windows_compat.py` l'applique.

**R-14** · **Tout `open()` / `read_text()` / `write_text()` déclare `encoding="utf-8"`.**
Sur un Windows français le défaut est cp1252 : lire un JSON contenant un accent lève
`UnicodeDecodeError` là-bas et nulle part ailleurs. Appliqué par un test.

**R-15** · Un réflexe ne fait que du **réversible**. Le matcher a raison la plupart du temps,
pas toujours ; le prix d'une erreur doit être une contrariété, pas un dossier supprimé.


---

## Environnement

```bash
.venv/bin/python main.py            # lancer
.venv/bin/python -m pytest          # 480 tests, sans micro/écran/clé API/réseau
.venv/bin/python -m core.telemetry  # ce que les modèles ont réellement coûté
.venv/bin/python -c "from core import desktop; print(desktop.report())"  # capacités système
.venv/bin/python -c "from core import local;   print(local.report())"    # pile hors-ligne
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

### `core/desktop` — une façade, trois backends (phase 04)

Aucun module d'action ne refait `if _OS == "Windows": … elif "Darwin": …`.

```python
from core import desktop
data, mime = desktop.screenshot()
desktop.brightness_set(60); desktop.trash(p); desktop.notify("Titre", "Corps")
print(desktop.report())        # la matrice de capacités de cette machine
```

Une capacité n'est pas un booléen : `Capability(nom, disponible, backend, détail)`.
Un « non » **doit** dire ce qui le corrigerait.

| Surface | Linux/Wayland | Windows | macOS |
|---|---|---|---|
| Capture | portail Screenshot v2 | mss | screencapture |
| Luminosité | logind `SetBrightness` | WMI/PowerShell | ✗ (aucune API scriptable) |
| Volume | wpctl/pactl | pycaw | osascript |
| Corbeille | portail Trash | send2trash | send2trash |
| Presse-papiers | pyperclip / wl-clipboard | pyperclip | pyperclip / pbcopy |
| Notification | notify-send | win10toast | osascript |
| Fond d'écran | portail Wallpaper | SystemParametersInfoW | osascript |
| Entrées | portail RemoteDesktop | SendInput | Quartz (permission) |

`mss` sous Wayland **ne plante pas** : il renvoie un rectangle noir. C'est pour ça que la
détection de session passe avant le choix du mécanisme.

### `core/desktop/input.py` — clavier et souris (phase 08)

Le chaînon qui manquait à R-09. `input_capability()` décrivait une capacité que la façade
n'offrait pas ; les actions n'avaient nulle part où aller, d'où 140 appels `pyautogui`
directs dans 9 modules. Ils sont tous partis.

```python
desktop.type_text("bonjour")          desktop.key("enter")
desktop.hotkey("ctrl", "shift", "escape")
desktop.click(); desktop.move_by(dx, dy); desktop.scroll(3)
desktop.screen_size()                 desktop.input_mechanism()  # "SendInput"
```

- **Un seul vocabulaire.** `win`, `super`, `cmd`, `command` sont la même touche ; un module
  d'action ne branche plus sur l'OS pour choisir le mot. Sur macOS `super` devient Command,
  parce que là-bas ce n'est pas une orthographe, c'est une autre touche.
- **Keysyms X11, pas keycodes.** Un keycode est une position physique : synthétiser `a` par
  keycode tape `q` sur un clavier AZERTY. Le keysym est la lettre.
- **`pyautogui.hotkey("ctrl", "nosuchkey")` ne lève rien** — `keyDown` sort silencieusement
  sur un nom inconnu, Ctrl descend, remonte, et l'appel annonce un succès. La façade valide
  contre `KEYBOARD_KEYS` avant d'appuyer (R-12).
- **Wayland : `libei` non, portail oui.** libei est installé (1.5.0) mais sa liaison Python
  passe par l'introspection GObject, que R-10 exclut. Le portail RemoteDesktop atteint le
  même chemin dans mutter — qui l'implémente *au-dessus* de libei — en D-Bus pur via jeepney.
- **Ce que Wayland ne fait toujours pas** : le pointage **absolu**. `NotifyPointerMotionAbsolute`
  exige un identifiant de flux PipeWire, donc une session ScreenCast liée — une seconde
  autorisation de *partage d'écran* pour bouger une souris. Non câblé, et `move_to()` le dit
  au lieu de déplacer le pointeur quelque part de plausible. Le clavier, les boutons, le
  mouvement relatif et le défilement fonctionnent : 117 des 140 appels repris.
- **La session coûte une boîte de dialogue.** Créée une fois, gardée sur sa propre connexion
  D-Bus, `persist_mode=2` et jeton de restauration sous `~/.config/alexio` pour que le
  deuxième lancement soit muet. Une session morte est jetée, pas contournée par XTEST.

### `core/tool_policy.py` — qui demande, qui agit (phase 09)

Trois pièces correctes qui ne se parlaient pas : `core/confirm.py` émettait un jeton
infalsifiable utilisé **à un seul endroit** ; `ToolSpec.irreversible` existait et n'était
peuplé que par un test ; et `_execute_tool` ne consultait ni l'un ni l'autre. Résultat :
`shutdown_jarvis` appelait `os._exit(0)` sur la parole du modèle, et `dev_agent` écrivait
du code généré, `pip install`ait les paquets que les traces de ce code nommaient, puis
l'exécutait.

```bash
.venv/bin/python -c "from core import tool_policy; import main; print(tool_policy.describe(main.TOOL_DECLARATIONS))"
```

Trois verdicts : `RUN` (réversible — agir maintenant, `push_undo`), `CONFIRM` (irréversible
— `core/confirm.py`), `SELF` (le module a sa propre porte ; deux bannières pour une demande,
l'utilisateur répond à la première et rien ne se passe). `SELF` est une affirmation sur le
code d'un autre module, donc le test la vérifie **contre sa source**.

`_sync_handlers` remplace 18 branches `elif` par une table de callables. Ce n'est pas une
question de lignes : un `await` en ligne ne peut pas être remis à `core/confirm.py` pour
être exécuté plus tard, un callable si.

Quatre outils demandent : `shutdown_jarvis`, `dev_agent`, `code_helper` en `run`/`build`,
`game_updater` avec `shutdown_when_done`. `code_helper` en `auto` repasse par la porte
**après** avoir résolu l'intention — sinon « lance ça » formulé en description était le seul
contournement de la règle.

`dev_agent` ne pose plus de paquets dans le venv d'Alexio : `_project_python()` crée un venv
par projet. Impossible → il **refuse** d'installer au lieu de retomber sur `sys.executable`.

### `core/local` — hors-ligne (phase 05)

- **`reflex`** — commandes courtes traitées **sans modèle**. Zéro dépendance, **0,27 ms**
  par décision, 8 intentions / 78 phrases FR+EN. Seules des actions **réversibles** ont le
  droit d'y figurer : `ReflexRouter.register()` refuse une intention `reversible=False`.
  Branché sur la saisie texte de `main.py`. **Pas** sur la voix : avec Gemini Live le
  transcript revient *du serveur*, le modèle génère déjà — il n'y a plus rien à gagner.
  Il faudra le STT local pour ça.
- **`speech`** — Parakeet TDT v3 (STT) et Piper (TTS), chargés paresseusement.
  Rien n'est installé par défaut : `pip install -r requirements-local.txt`.
  *Pas* faster-whisper (~3× temps réel contre ~30×), *pas* Kokoro (3,6 s et 2 Go de pic).

### `dashboard/` — la seule surface exposée au réseau (phase 06)

Tout le reste d'Alexio échoue à l'abri parce qu'il est injoignable. Le dashboard écoute sur
`0.0.0.0` : ses bugs sont atteignables par n'importe qui sur le réseau.

- **`dashboard/auth.py`** — `CredentialStore` : tout ce qui est distribué et sa durée de vie.
  Aucune dépendance hors bibliothèque standard, et l'horloge est injectable — une expiration
  qu'on ne peut vérifier qu'en attendant trente jours est une expiration que personne ne
  vérifie. `tests/test_dashboard_security.py` la déplace.
- La clé de 6 caractères **apparie**, elle ne chiffre pas. L'appariement rend 256 bits
  aléatoires ; plus rien n'est dérivé d'une chaîne saisie.
- Durées : session 12 h glissantes / 7 j absolus · appareil 30 j absolus / 7 j d'inactivité,
  8 maximum, stockés hachés · ticket WebSocket 30 s à usage unique.
- Étranglement : 5 échecs par adresse et par minute ; à 20 échecs globaux en 5 minutes,
  **toutes les clés en attente sont brûlées**. Une limite par adresse ne protège pas 30 bits.
- CryptoJS est épinglé par son empreinte SHA-512 (SRI cdnjs, vérifiée le 2026-09-07) et
  téléchargé depuis `serve()`, jamais à l'import.

**R-16** · **Cookie = navigation. En-tête `Authorization` = API.** Jamais l'inverse, jamais
les deux. Un site tiers peut faire envoyer un cookie par le navigateur ; il ne peut pas lui
faire poser un en-tête. Séparer les deux ne *défend* pas contre le CSRF, il le rend
impossible — et c'est ce qui autorise le cookie qui garde `/` côté serveur.

**R-18** · **Le dispatch consulte la porte.** Aucun outil ne s'exécute sans que
`core/tool_policy.classify()` ait rendu un verdict, et tout outil livré a une règle
explicite — pas le défaut. Appliquée par `tests/test_tool_policy.py`.
Le défaut pour un outil *inconnu* est `RUN`, délibérément : une porte qui demande pour tout
apprend à l'utilisateur à cliquer oui sans lire, ce qui ressemble à un consentement sans en
être un. La liste `CONFIRM` est plafonnée à 4 par un test.

**R-17** · **Rien ne demande l'élévation au démarrage.** Ni UAC, ni `pkexec`, ni `sudo`, ni
`osascript … with administrator privileges`. Un programme qui réclame les droits admin par
effet de bord apprend à l'utilisateur à dire oui. Un « non » imprime la commande exacte.

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

Corrigé en phase 06 : la clé AES dérivée de la clé de 6 caractères (SHA-256 + sel fixe dans
la source, ~2^29,7 candidats), les jetons d'appareil éternels, la page `/` gardée en
JavaScript, les quatre chemins d'élévation du pare-feu, l'absence de compteur d'échecs, et
le bundle CryptoJS téléchargé à l'import sans contrôle d'intégrité.

Corrigé en phase 08 : `from playwright.async_api import …` **sans garde** dans
`actions/browser_control.py`, importé au niveau module par `main.py` — sur une installation
neuve, Alexio ne démarrait pas du tout. Même classe que les cinq gardes `pyautogui` de la
phase 00, ratée parce que l'audit cherchait des gardes *trop étroites*, pas des imports
*sans garde*. Avec elle, dix `except ImportError` là où le paquet lève autre chose
(pyperclip, cv2, mss, win32com), les 1 259 lignes mortes de `core/llm_client.py`,
`core/tts.py`, `core/stt.py`, `core/installer.py`, et les quatre `asyncio.get_event_loop()`
dans des coroutines. `tests/test_optional_dependencies.py` retire chaque paquet optionnel un
par un — puis tous d'un coup — et importe l'application entière.

Reste :

- Le classifieur d'intention (classe « Réflexe », < 200 ms sans modèle) reste à faire —
  il vit avec le mot de réveil en phase 05.
- `computer_settings.volume_get()` renvoie `None` sur cette machine Windows alors que
  `desktop.capabilities()` annonce `yes volume pycaw` : `AudioUtilities.GetSpeakers()` rend
  un `AudioDevice` sans `.Activate` dans le pycaw installé. Une capacité qui dit oui et
  répond None, c'est exactement ce que R-06 interdit — à corriger des deux côtés.
- Le pointage **absolu** sous Wayland (`move_to`, `click(x, y)`) demande une session
  ScreenCast liée pour l'identifiant de flux PipeWire. Refusé explicitement, pas contourné.
- **R-03 n'est pas tenue par trois modules.** `file_processor`, `flight_finder` et
  `game_updater` ont `(parameters, player, speak)` au lieu de
  `(parameters, response, player, session_memory, speak)`. C'est pour ça que
  `_sync_handlers` passe des mots-clés différents selon l'outil au lieu d'un appel uniforme.
  `tests/test_actions.py` vérifie que le dispatch appelle chacun avec des arguments qu'il
  accepte réellement — mais uniformiser les trois signatures reste à faire.
- `actions/` reste largement non couvert : `tests/test_actions.py` couvre le contrat d'appel,
  `file_controller` en entier et les deux résolveurs purs. Les 16 autres modules n'ont que
  le contrat.
- `computer_settings._detect_action("increase the brightness")` rend `""`. La formulation
  la plus naturelle en anglais ne résout pas ; « brighter » et « brightness up » oui.
  Ce n'est pas un plantage — le vide déclenche `_suggest()`, un aller-retour qui nomme de
  vrais candidats — mais c'est un aller-retour évitable. Il manque des alias.
- `dev_agent` exécute toujours `plan["run_command"]`, une chaîne écrite par le modèle,
  découpée sur les espaces. Confirmé et dans un venv de projet désormais, mais le contenu
  reste choisi par le modèle.
- `config/certs/jarvis.key` est toujours dans l'historique Git (commit `067f528`). La clé
  locale n'est **plus** celle qui a fuité — `_ensure_certs()` en a régénéré une — donc rien
  ne sert aujourd'hui une clé publiquement connue. Reste à purger l'historique :
  `git filter-repo --invert-paths --path config/certs/jarvis.key` puis `push --force`.
  Réécrit 50 SHA sur un dépôt public : c'est une décision, pas une correction.
- 480 tests, mais aucune CI ne les lance encore.
- `ui.py` : 4 158 lignes, 20 classes, un fichier — jamais touché par la refonte, et il
  contient les seuls chemins de confirmation visuels. Toute extension de R-04 y passe.
- `_base_dir()` est réécrit dans 15 fichiers (→ `core/paths.py`), et il reste 109
  `except …: pass` (aucun `except:` nu, ce qui est déjà ça).
- **Le chemin Wayland de `core/desktop/input.py` n'a jamais tourné sur un compositeur.**
  Il est écrit contre la spécification du portail RemoteDesktop et couvert par une doublure
  qui enregistre ce que le compositeur recevrait ; le chemin Windows/SendInput, lui, a été
  exécuté pour de vrai. Première frappe sous GNOME 49 = première mesure.
- Alexio n'a **jamais été exécuté sous Windows** depuis la refonte. La phase 07 vérifie que
  rien ne l'en empêche ; ce n'est pas la même chose que de l'avoir vu tourner.

---

## Conventions de code

- Commentaires denses expliquant le *pourquoi* d'une décision — c'est le style du dépôt, le garder.
- Alignement vertical des dicts et assignations (style existant).
- Messages d'erreur : ce qui a échoué + comment le corriger. Pas d'excuses.




Trois choses trouvées, pas corrigées
R-03 n'est pas tenue. file_processor, flight_finder et game_updater ont (parameters, player, speak) au lieu de la signature complète. C'est pour ça que _sync_handlers passe des mots-clés différents par outil. Le test vérifie que le dispatch est cohérent avec la réalité ; uniformiser les trois signatures reste à faire.

_detect_action("increase the brightness") rend "". La formulation la plus naturelle en anglais ne résout pas ; « brighter » et « brightness up » oui. Pas un plantage — le vide déclenche _suggest() — mais un aller-retour évitable. Il manque des alias.

dev_agent exécute toujours plan["run_command"], une chaîne écrite par le modèle, découpée sur les espaces. Confirmé et dans un venv de projet maintenant, mais le contenu reste choisi par le modèle.

Reste de ton §3 : ui.py (4 158 lignes), core/paths.py pour les 15 _base_dir, et les 109 except: pass. Noté dans la dette — dis-moi si j'attaque.
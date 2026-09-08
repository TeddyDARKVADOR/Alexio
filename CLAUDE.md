# Alexio — règles de développement

Assistant vocal, fork de **Mark LII** (FatihMakes, CC BY-NC 4.0 — usage personnel, attribution
obligatoire). Cible : **Fedora 43 · GNOME 49.9 · Wayland · i5-8265U · pas de GPU**.

Refonte en cours : passer d'un système câblé sur Gemini Live à une architecture multi-modèle
routée. Dossier d'architecture complet : voir l'artifact « Refonte Alexio ».

---

## Architecture

Deux plans distincts, jamais fusionnés en une seule fonction :

| Plan                 | Interface    | Nature                                        | État                      |
| -------------------- | ------------ | --------------------------------------------- | ------------------------- |
| **A — Conversation** | `core/voice` | session persistante, audio bidirectionnel, ms | **fait (phase 02)**       |
| **B — Requête**      | `core/ai`    | sans état, annulable, s                       | **fait (phases 01 + 03)** |

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
prévient avant de raccrocher, et `main.py` reconstruit la session sur ce signal.

**Cycle de vie mesuré sur le fil** (session réelle, 13 min, 2026-09-08 — ne pas re-deviner
ces chiffres, ils sont dans `logs` du banc d'essai) :

| Fait                    | Mesure                                                                    |
| ----------------------- | ------------------------------------------------------------------------- |
| `goAway`                | à **9 min 01 s**, `{"timeLeft": "50s"}` — une **chaîne**, durée protobuf   |
| Fermeture après goAway  | **aucune** : la socket a vécu 3 min de plus, goAway est un avis            |
| Keepalive               | 39 pings / 39 pongs, toutes les 20 s, aucun perdu                          |
| Lag de boucle           | 11 ms au pire (`ping_timeout` est à 20 000 ms)                             |
| `sessionResumptionUpdate` | un **vide** immédiatement, puis un vrai `newHandle` + `resumable: true` ~7 s après le premier tour complet |
| Consommateur bloqué 35 s | **ne tue pas** la connexion : le keepalive est une tâche asyncio à part    |

Trois conséquences pour le code :

- `ping_interval` et `ping_timeout` valent 20 s (défauts `websockets`, que le SDK ne change
  pas). Un « keepalive ping timeout » n'a donc que **deux** causes : le réseau s'est tu, ou
  ce processus a bloqué sa propre boucle 20 s (R-07). `_LoopLag` dans `main.py` mesure la
  seconde en continu pour que la coupure suivante nomme sa cause au lieu de lister les deux.
- Un échec de transport ne quitte plus `core/voice` sous sa forme `websockets` : il devient
  un `SessionClosed` qui porte `code`, `reason` et **`by`** (`"server"` / `"client"`).
  `describe()` distingue « le serveur a terminé la session » de « c'est nous qui avons
  abandonné », deux causes opposées derrière un symptôme identique.
- `main.py` ne promet de garder la conversation que s'il a réellement un handle.

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
- **`media`** — images _et_ audio ; le type MIME décide de la capacité requise.
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
avant tout appel. Elle juge sur les _arguments_ : `file_controller` qui lit et
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

| Surface        | Linux/Wayland            | Windows               | macOS                     |
| -------------- | ------------------------ | --------------------- | ------------------------- |
| Capture        | portail Screenshot v2    | mss                   | screencapture             |
| Luminosité     | logind `SetBrightness`   | WMI/PowerShell        | ✗ (aucune API scriptable) |
| Volume         | wpctl/pactl              | pycaw                 | osascript                 |
| Corbeille      | portail Trash            | send2trash            | send2trash                |
| Presse-papiers | pyperclip / wl-clipboard | pyperclip             | pyperclip / pbcopy        |
| Notification   | notify-send              | win10toast            | osascript                 |
| Fond d'écran   | portail Wallpaper        | SystemParametersInfoW | osascript                 |
| Entrées        | portail RemoteDesktop    | SendInput             | Quartz (permission)       |

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
  même chemin dans mutter — qui l'implémente _au-dessus_ de libei — en D-Bus pur via jeepney.
- **Ce que Wayland ne fait toujours pas** : le pointage **absolu**. `NotifyPointerMotionAbsolute`
  exige un identifiant de flux PipeWire, donc une session ScreenCast liée — une seconde
  autorisation de _partage d'écran_ pour bouger une souris. Non câblé, et `move_to()` le dit
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
  transcript revient _du serveur_, le modèle génère déjà — il n'y a plus rien à gagner.
  Il faudra le STT local pour ça.
- **`speech`** — Parakeet TDT v3 (STT) et Piper (TTS), chargés paresseusement.
  Rien n'est installé par défaut : `pip install -r requirements-local.txt`.
  _Pas_ faster-whisper (~3× temps réel contre ~30×), _pas_ Kokoro (3,6 s et 2 Go de pic).

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
faire poser un en-tête. Séparer les deux ne _défend_ pas contre le CSRF, il le rend
impossible — et c'est ce qui autorise le cookie qui garde `/` côté serveur.

**R-18** · **Le dispatch consulte la porte.** Aucun outil ne s'exécute sans que
`core/tool_policy.classify()` ait rendu un verdict, et tout outil livré a une règle
explicite — pas le défaut. Appliquée par `tests/test_tool_policy.py`.
Le défaut pour un outil _inconnu_ est `RUN`, délibérément : une porte qui demande pour tout
apprend à l'utilisateur à cliquer oui sans lire, ce qui ressemble à un consentement sans en
être un. La liste `CONFIRM` est plafonnée à 4 par un test.

**R-17** · **Rien ne demande l'élévation au démarrage.** Ni UAC, ni `pkexec`, ni `sudo`, ni
`osascript … with administrator privileges`. Un programme qui réclame les droits admin par
effet de bord apprend à l'utilisateur à dire oui. Un « non » imprime la commande exacte.

---

## `./jarvis` — l'interface de développement

```bash
./jarvis help                     # les commandes
./jarvis check                    # avant chaque commit — 4,4 s, pas la suite
./jarvis test --group security
./jarvis test --failed
./jarvis coverage core.ai.router
./jarvis rules --explain JAR012
./jarvis audit --critical
./jarvis health
```

`check` est statique : parse, noms (pyflakes), les 20 règles, la baseline, le
registre. **Il ne lance pas la suite** — une vérification de pré-commit qui prend
une minute est une vérification que personne ne lance, et les garde-fous ne
tirent alors qu'en CI, où ils coûtent un aller-retour au lieu d'une frappe.
`check` trouve le `NameError` de `screen_processor.py:144` en 4 s.

Le script épingle `.venv/bin/python` délibérément : le Python système est en
3.14 et l'app tourne sur le venv 3.11. Un outil qui mesure un autre arbre que
celui qui s'exécute ne mesure rien.

## `tools/jarvis_lint` — les invariants, appliqués

```bash
.venv/bin/python -m tools.jarvis_lint            # ce qui est neuf depuis la baseline
.venv/bin/python -m tools.jarvis_lint --all      # tout, baseline comprise
.venv/bin/python -m tools.jarvis_lint --rules    # à quoi sert chaque règle
```

L'audit du 2026-09-08 a trouvé 21 défauts. **La moitié n'étaient pas des erreurs ordinaires :**
c'étaient des endroits où une règle écrite ici n'avait aucune contrepartie mécanique dans le
code. R-12 disait « lève une erreur » et `core/ai/__init__.py` avalait la levée ; R-18 disait
« le dispatch consulte la porte » et la branche plugin ne la consultait pas ; R-02 disait
« aucun identifiant de modèle hors `core/ai/` » et deux modules d'action en épinglaient un —
personne ne l'avait jamais vu parce que `tests/test_tool_wiring.py` ne vérifie que les
_imports_ de SDK, pas les identifiants.

Une règle qui n'existe qu'en prose est une règle que le prochain changement casse en silence,
que ce changement vienne d'une personne ou d'un agent. Onze règles vivent donc maintenant dans
`tools/jarvis_lint/`, et elles font échouer un test.

| Règle  | Ce qu'elle interdit                                         | Née de |
| ------ | ----------------------------------------------------------- | ------ |
| JAR001 | écrire via un chemin lu dans un document du modèle sans confinement | `dev_agent` écrivait sur `/etc/` |
| JAR002 | un identifiant de modèle hors `core/ai/` (R-02)             | `dev_agent`, `code_helper` |
| JAR003 | exécuter un outil sans verdict de `tool_policy` (R-18)      | la branche plugin |
| JAR004 | avaler une contrainte non satisfaite (R-12)                 | `Budget.private()` partait chez Google |
| JAR005 | lever un drapeau d'état sans `finally`                      | `_vision_busy` bloqué à vie |
| JAR006 | jeter le `Future` d'un `run_in_executor`                    | `dashboard/server.py` |
| JAR007 | une route GET qui change l'état                             | `/auto-login` dépense le PIN |
| JAR008 | un dict indexé par l'appelant sans plafond (dashboard)      | `Throttle._fails` |
| JAR009 | rendre l'état mutable d'une spec par référence              | `ToolSpec.to_anthropic` |
| JAR010 | relire tout le journal sur le chemin chaud                  | 491 ms par `ai.generate()` |
| JAR011 | `except ImportError` sur un paquet à extension native       | 11 gardes étroites |
| JAR012 | `shell=True` avec une commande assemblée à l'exécution       | `open_app` — injection depuis un paramètre d'outil |
| JAR013 | une ligne de commande produite par `.split()`                | `dev_agent._run_project` |
| JAR014 | `tempfile.mktemp`                                            | 5 sites |
| JAR015 | le verdict de `tool_policy` obtenu puis jeté                 | rien — le bug d'après |
| JAR016 | détruire sans enregistrer d'undo (R-05)                      | 7 appels, 4 modules |
| JAR017 | comparer un secret avec `==`                                 | rien — prophylactique |
| JAR018 | un secret dans un log                                        | rien — prophylactique |
| JAR019 | vérification de certificat désactivée                        | rien — prophylactique |
| JAR020 | un verdict RUN qui invoque un bac à sable non testé          | `desktop_control` |

## `tools/jarvis_health` — trois mesures, jamais interchangeables

```bash
./jarvis health          # le score composite
./jarvis audit --tree    # couverture contre garanties, par module
```

**Couverture de code** = cette ligne a-t-elle été exécutée. **Couverture de
comportement** = cette garantie est-elle vérifiée. **Couverture de règles** =
cet invariant est-il mécaniquement imposé. Les trois ne se remplacent pas :
`core/ai/router.py` est à 97 % de lignes, 93 % de branches, et `Budget.private`
mesure **100 %** pendant que la garantie « ne quitte pas la machine » est cassée.
`test_revoking_devices_actually_revokes_them` passe et ne vérifie pas que les
sessions meurent.

**Une violation critique plafonne le score, elle ne le grignote pas.** Un poids
se dilue — ajoutez vingt modules bien testés et n'importe quel seuil redevient
atteignable. Un plafond, non : une garantie critique cassée et le score ne peut
pas dépasser 49. `tests/test_health_ledger.py` vérifie la propriété.

Le registre (`behaviors.py`) est écrit à la main — il le faut, aucun outil ne
peut deviner que la question intéressante sur `core/ai/__init__.py` est de savoir
si `Budget.private()` refuse encore. Il est donc tenu à la même discipline que la
baseline : `reconcile()` échoue dans les deux sens, et un identifiant de test
inexistant est une erreur, pas une dégradation silencieuse en « non prouvée ».

**La baseline n'est pas une liste d'exemptions.** Elle a été posée à 46 violations le jour
de l'installation des règles — exempter les défauts existants était le seul moyen de ne pas
rendre le linter vert par décret. Elle est **à 6**. Le test échoue sur tout ce qui n'y est pas,
et corriger un défaut = supprimer sa ligne.
Corriger un défaut = supprimer une ligne du fichier, et une entrée qui ne se reproduit plus est
elle-même une erreur (`--check-stale`) — sinon la prochaine occurrence réelle du même défaut
serait pardonnée en silence.

**Chaque règle a un test positif _et_ un test négatif**, et un méta-test refuse une règle qui
n'aurait que l'un des deux. Le négatif est le porteur : JAR009 épinglait d'abord un `__repr__`,
JAR004 le repli entre providers pourtant documenté, JAR001 quatorze écritures de fichier
ordinaires. Une règle sans test négatif est une règle dont personne n'a vérifié les faux
positifs — et un linter qui crie au loup finit en liste d'exemptions, c'est-à-dire en prose.

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
phase 00, ratée parce que l'audit cherchait des gardes _trop étroites_, pas des imports
_sans garde_. Avec elle, dix `except ImportError` là où le paquet lève autre chose
(pyperclip, cv2, mss, win32com), les 1 259 lignes mortes de `core/llm_client.py`,
`core/tts.py`, `core/stt.py`, `core/installer.py`, et les quatre `asyncio.get_event_loop()`
dans des coroutines. `tests/test_optional_dependencies.py` retire chaque paquet optionnel un
par un — puis tous d'un coup — et importe l'application entière.

Corrigé au **premier vrai lancement sous GNOME 49 / Wayland** (2026-09-08). Cinq défauts,
dont aucun n'était visible sans exécuter l'application sur cette machine :

- **Le corps D-Bus des appels portail était mal construit.** jeepney sérialise un `a{sv}`
  depuis un **dict simple** dont les _valeurs_ sont des variantes `(signature, valeur)` ;
  le dict lui-même n'en est pas une. Le code l'emballait en `("a{sv}", options)` — un corps
  qui a l'air correct et meurt dans le sérialiseur sur `Not suitable for array`. Toute
  capture d'écran échouait. La même ligne relisait les options de l'appelant en
  `body[-1][1]`, ce qui sur un dict n'est pas une erreur mais un manque
  (`isinstance({}, tuple)` est faux) : `interactive`, `types`, `persist_mode` et
  `restore_token` étaient **jetés avant l'envoi**.
  Le bug était écrit **deux fois** — `portal.call()` et `_RemoteDesktop._request()` dans
  `input.py`, qui ne peut pas réutiliser `call()` parce qu'il doit rester sur sa propre
  connexion. L'injection du `handle_token` vit désormais dans `portal.with_handle_token()`,
  seule et partagée. Le chemin clavier/souris Wayland serait mort exactement là, à la
  première frappe.
- **40 lignes d'ALSA/PortAudio à chaque démarrage.** Échouer à ouvrir un flux est la
  _mesure_ de `_usable()` (R-06), pas un accident ; mais chaque refus fait imprimer à
  PortAudio cinq lignes de références au source C **sur le descripteur 2**, que
  `contextlib.redirect_stderr` ne voit pas. `_quiet_c_stderr()` redirige au niveau fd,
  autour des ouvertures individuelles et pas de l'énumération entière — la fenêtre est
  process-wide tant qu'elle dure.
- **La sonde d'entrée échouait sur un transitoire.** Le plugin ALSA de PipeWire ne rend pas
  l'endpoint à l'instant où `_usable()` le referme, et la sonde de transport le rouvrait
  quelques millisecondes plus tard : `-9985 Device unavailable`. La direction retombait sur
  « any host API » et le sélecteur annonçait un endpoint différent de celui qui marche. Une
  seule reprise, 200 ms, réservée au transitoire.
- **`response.data` du SDK Gemini** journalise un avertissement dès qu'un tour porte autre
  chose que de l'audio — ce qu'un modèle natif-audio avec pensée envoie en permanence.
  `_audio_of()` lit les parts directement : plus d'avertissement, et l'abandon des parts
  `text`/`thought` devient explicite au lieu d'incident.
- **`str()` sur l'`ExceptionGroup` d'un TaskGroup** vaut « unhandled errors in a TaskGroup
  (1 sub-exception) » — une phrase qui ne contient aucun des mots que les branches de
  diagnostic de `run()` cherchent. Toutes ces branches étaient donc **aveugles à ce qui
  échoue dans une session**, c'est-à-dire là où les sessions échouent. `_error_text()`
  aplatit le groupe _et_ la chaîne `__cause__`. Au passage : une socket qui cesse de
  répondre aux pings n'est pas une panne, c'est la fin ordinaire d'une longue conversation
  — une ligne, pas une trace de neuf cadres, et pas de backoff croissant.

Trouvé en corrigeant : `tests/test_optional_dependencies.py` faisait
`ROOT.rglob("core/**/*.py")`, et `rglob` ancre le motif à **n'importe quelle profondeur** —
il importait donc `.venv/…/numpy/core/` comme s'il faisait partie d'Alexio. Seize tests en
échec qui nommaient un paquet tiers et ne disaient rien sur le projet. `glob`, pas `rglob`.

Corrigé le 2026-09-08, en cherchant pourquoi la session se coupe en cours de route :

- **`_seconds("50s")` rendait `None`.** `LiveServerGoAway.time_left` est typé
  `Optional[str]` et le serveur envoie une durée protobuf — `"50s"`, le `s` final fait
  partie du format. L'ancien parseur essayait `total_seconds`, puis `seconds`, puis
  `float(value)` : une chaîne n'a ni l'un ni l'autre et `float("50s")` lève. **La seule
  forme réellement envoyée était la seule non gérée**, donc chaque avertissement de go-away
  arrivait sans son compte à rebours — le seul chiffre actionnable du message. Le test
  existant couvrait l'objet, le nombre et `None` ; pas la chaîne. C'est exactement pour ça
  qu'il a survécu.
- **L'exception `websockets` fuyait jusqu'à `main.py`**, qui classait la coupure par
  sous-chaînes. « Connection dropped » couvrait indistinctement trois évènements sans
  rapport : le serveur qui termine normalement (1000/1001), nous qui abandonnons faute de
  pong (1011), la socket qui disparaît (1006). Trois causes, trois correctifs, une seule
  ligne — c'est ainsi qu'une coupure récurrente reste inexpliquée. Voir `SessionClosed`.
- **Le message de go-away mentait par défaut** : il annonçait « sans perdre la
  conversation » sans vérifier qu'un handle existait.

Non reproduit, et il faut le dire : **la coupure prématurée elle-même.** Elle n'est
apparue ni sur 13 min de session nue, ni sous un blocage de 35 s du consommateur. Ce qui
reste à distinguer — réseau contre boucle bloquée — est désormais mesuré en continu par
`_LoopLag`, et la prochaine occurrence l'imprimera d'elle-même.

### Corrigé le 2026-09-08, après l'audit des 25 défauts

Vingt et un des vingt-cinq, chacun avec un test qui aurait échoué avant. Les tests de
comportement vivent dans `tests/test_security_invariants.py` — ils vérifient les *phrases*
que la documentation promet à l'utilisateur, pas les unités.

- **R-12 rétablie.** `_resolve` avalait `NoModelFits` et la boucle en dessous rajoutait tous
  les providers joignables, donc `Budget.private()` partait chez Gemini, Anthropic et OpenAI
  dans exactement le cas où la garantie existe. Elle lève désormais — sauf pour une requête
  **sans contrainte**, où élargir reste correct, et c'est toute la différence entre un repli
  et une promesse cassée.
- **`core/paths.py`.** `safe_join(root, tail)` : pathlib jette la base quand la queue est
  absolue, donc `project_dir / plan["path"]` n'était pas un contrôle. `dev_agent` écrivait
  où le planificateur demandait. `~` est refusé plutôt que résolu — son sens dépend de qui
  l'expanse.
- **La porte couvre les plugins (R-18).** La branche plugin ne consultait rien, et
  `tool_policy.register()` n'existe que pour eux.
- **Injection de commande.** `open_app._launch_windows` validait un *préfixe*
  (`which(app_name.split(".")[0])`) puis exécutait la chaîne entière au shell.
  `app_name` vient du modèle. Plus aucun `shell=True` : `which()` résout un chemin, lancé
  comme argv[0].
- **Le routeur compare enfin la même statistique** : `latency_ms` rend le p50 mesuré face au
  `typical_latency_ms` déclaré. Le p95 reste disponible sous `latency_p95_ms`, séparément.
- **`revoke_devices()` tue les sessions ouvertes** — et le cookie de navigation, et les
  tickets. `test_revoking_devices_actually_revokes_them` passait depuis toujours : il
  vérifiait que le dictionnaire était vidé.
- **La télémétrie ne relit plus tout** : 491 ms → 104 ms par décision de routage, en bornant
  la *lecture* et pas la liste rendue.
- **Le bac à sable de `desktop_control` est testé** — la phrase qui justifie d'exécuter du
  code du modèle sans confirmation ne coûtait rien à personne.
- Plus : le `NameError` de `screen_processor`, le drapeau vision bloqué à vie, la bannière de
  confirmation qui restait après expiration, le `Throttle` non borné, `/auto-login` qui
  dépensait le PIN sur un GET, les `Future` jetées, `ToolSpec` qui rendait son état par
  référence, les 5 `mktemp`, les 11 gardes d'import étroites, `run_command.split()` →
  `shlex.split`, les identifiants de modèle morts (R-02), le contexte R-03 des plugins, le
  hash SHA-512 à chaque requête, le backoff local mort, et R-05 rétablie sur les deux
  réorganisateurs de bureau.

Reste :

- **R-05 n'est pas tenue par cinq appels** restants (`reminder` ×3, `code_helper`,
  `game_updater`) — tous des suppressions de fichiers que la fonction vient d'écrire, donc du
  ménage plutôt que de la perte. Dans la baseline JAR016, à trancher un par un.
- `_vision_close_pending` est levé sans `finally` dans `_receive_audio` : le drapeau est
  délibérément passé au tour suivant, et une reconnexion le remet à zéro. Baseline JAR005.
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
- **Le chemin Wayland de `core/desktop/input.py` n'a toujours pas tourné sur un
  compositeur.** Il est écrit contre la spécification du portail RemoteDesktop et couvert
  par une doublure qui enregistre ce que le compositeur recevrait ; le chemin
  Windows/SendInput, lui, a été exécuté pour de vrai. Le défaut de sérialisation D-Bus
  ci-dessus a été corrigé et les cinq corps (`CreateSession`, `SelectDevices`, `Start`,
  `Screenshot`, `SetWallpaperURI`) sont vérifiés contre le sérialiseur jeepney — mais
  sérialiser n'est pas frapper. Créer la session ouvre une boîte de dialogue de partage et
  la frappe suivante part dans la fenêtre qui a le focus : ça se teste à la main, pas depuis
  un agent. Première frappe sous GNOME 49 = première mesure.
- Alexio n'a **jamais été exécuté sous Windows** depuis la refonte. La phase 07 vérifie que
  rien ne l'en empêche ; ce n'est pas la même chose que de l'avoir vu tourner.

---

## Conventions de code

- Commentaires denses expliquant le _pourquoi_ d'une décision — c'est le style du dépôt, le garder.
- Alignement vertical des dicts et assignations (style existant).
- Messages d'erreur : ce qui a échoué + comment le corriger. Pas d'excuses.

Trois choses trouvées, pas corrigées
R-03 n'est pas tenue. file_processor, flight_finder et game_updater ont (parameters, player, speak) au lieu de la signature complète. C'est pour ça que \_sync_handlers passe des mots-clés différents par outil. Le test vérifie que le dispatch est cohérent avec la réalité ; uniformiser les trois signatures reste à faire.

\_detect_action("increase the brightness") rend "". La formulation la plus naturelle en anglais ne résout pas ; « brighter » et « brightness up » oui. Pas un plantage — le vide déclenche \_suggest() — mais un aller-retour évitable. Il manque des alias.

dev_agent exécute toujours plan["run_command"], une chaîne écrite par le modèle, découpée sur les espaces. Confirmé et dans un venv de projet maintenant, mais le contenu reste choisi par le modèle.

Reste de ton §3 : ui.py (4 158 lignes), core/paths.py pour les 15 \_base_dir, et les 109 except: pass. Noté dans la dette — dis-moi si j'attaque.

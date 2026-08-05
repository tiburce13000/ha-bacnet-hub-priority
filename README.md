# BACnet Hub Priority

> **Nom du dépôt** : `ha-bacnet-hub-priority` — **Intégration** : `BACnet Hub Priority`
> *(le `domain` interne reste `bacnet_hub` : compatibilité totale des entités avec l'intégration d'origine)*


Fork de [**magliaral/ha-bacnet-hub**](https://github.com/magliaral/ha-bacnet-hub) qui ajoute
un **sélecteur de priorité d'écriture BACnet** (device-level) à chaque device découvert.

Ce fork combine :
- la **découverte et la lecture** robustes de l'intégration originale de **@magliaral** ;
- le **contrôle de la priorité d'écriture** (BACnet Priority Array), dont le concept est
  repris de l'intégration de **@CervezaStallone**.

> 📄 La documentation d'origine complète de l'intégration est conservée dans
> [`README_ORIGINAL_magliaral.md`](README_ORIGINAL_magliaral.md).

---

## 🙏 Remerciements

Ce fork n'existe que grâce au travail de deux développeurs de la communauté open source,
que je remercie chaleureusement :

- **[Alessio Magliarella (@magliaral)](https://github.com/magliaral/ha-bacnet-hub)** —
  auteur de l'intégration **BACnet Hub** d'origine, qui constitue **toute la base** de ce
  fork (découverte des devices, import des points, lecture temps réel, publication HA↔BACnet).
  Licence d'origine : MIT.

- **[CervezaStallone](https://github.com/CervezaStallone/Home-Assistant-BACnet-integration)** —
  auteur de l'intégration **Home Assistant BACnet**, dont le **concept de sélecteur de
  priorité d'écriture device-level** a inspiré la fonctionnalité ajoutée ici.
  Licence : GPL v3.

Tout le mérite de l'architecture revient à ces deux auteurs. Ce fork se contente d'assembler
et d'adapter leurs approches pour un besoin précis (piloter la ventilation d'un automate
Distech au niveau de priorité « Manual Operator »).

---

## ➕ Ce qui a été ajouté / modifié dans ce fork

Par rapport à l'intégration originale de @magliaral :

### Nouveauté fonctionnelle
- **Entité `select` « Priorité d'écriture »** créée pour chaque device BACnet découvert
  (device-level, comme chez @CervezaStallone).
  - **Désactivée par défaut** (catégorie *Configuration*) — à activer manuellement.
  - Options : **8 à 16** (8 = *Manual Operator*).
  - **Défaut : 16** — donc **comportement d'origine strictement inchangé** tant que
    l'utilisateur n'active ni ne modifie ce sélecteur.
  - La priorité choisie **persiste** après redémarrage (`RestoreEntity`).
  - Quand elle est réglée sur une valeur, **toutes les écritures commandables** vers ce
    device (objets `ao`, `bo`, `av`, `bv`, `mv` disposant d'un Priority Array) utilisent
    cette priorité.

### Détail technique des changements
| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/write_priority.py` | **ajout** | Stockage/lecture de la priorité par device (dans `hass.data`). |
| `custom_components/bacnet_hub/write_priority_entity.py` | **ajout** | Entité `select` « Priorité d'écriture » (device-level, RestoreEntity). |
| `custom_components/bacnet_hub/select.py` | **modif** | Création d'une entité priorité par device découvert. |
| `custom_components/bacnet_hub/client_point_entities.py` | **modif** | La priorité d'écriture est **lue depuis le sélecteur** au lieu d'être figée à 16 ; portée étendue à `ao/bo/av/bv/mv`. |
| `manifest.json`, `hacs.json` | **modif** | Identité du fork. |

### Corrections de compatibilité Home Assistant 2025.12+

Deux problèmes du code d'origine devenus **bloquants** avec les versions récentes de
Home Assistant sont corrigés :

- **Thread-safety** : `_schedule_rescan` appelait `hass.async_create_task` depuis un thread
  autre que l'event loop, ce qui lève désormais une `RuntimeError` et empêche le chargement
  de l'intégration (et provoquait une tempête d'erreurs UDP `sendto` sur transport fermé).
  Remplacé par `hass.create_task` (variante thread-safe).
- **`via_device` inexistant** : les entités clientes référençaient un device parent
  (`(DOMAIN, entry_id)`) jamais déclaré dans le registre. Ce device « BACnet Hub » est
  maintenant créé au début de `async_setup_entry`, avant la création des entités enfants.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/sensor.py` | **modif** | `hass.create_task` (thread-safe) dans `_start_bg_task`. |
| `custom_components/bacnet_hub/__init__.py` | **modif** | Déclaration du device hub parent dans le registre. |

### Bouton « Relâcher » (libération du Priority Array)

Chaque point commandable disposant d'un Priority Array reçoit un **bouton « Relâcher »**
(`button`, désactivé par défaut, catégorie *Configuration*).

Un appui écrit **Null** dans le Priority Array **à la priorité configurée** : Home Assistant
libère le niveau qu'il occupait et **l'automate reprend la main** avec sa propre valeur
(inscrite à un niveau inférieur).

C'est le pendant indispensable du sélecteur de priorité :
- rendre la main à l'automate en fin de mode (ex. sortie d'un mode saisonnier) ;
- sécurité : si une sonde de référence devient indisponible, on relâche la commande
  au lieu de laisser une valeur figée.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/release_button_entity.py` | **ajout** | Entité bouton « Relâcher ». |
| `custom_components/bacnet_hub/button.py` | **ajout** | Plateforme `button` (création par point commandable). |
| `custom_components/bacnet_hub/__init__.py` | **modif** | Ajout de `button` à `PLATFORMS`. |
| `custom_components/bacnet_hub/client_point_entities.py` | **modif** | Méthode `_async_release_present_value()` (écriture Null à la priorité configurée). |

### Stabilité des entités (correction du « yoyo » COV)

Les entités importées pouvaient passer par intermittence à *indisponible* : la souscription
COV était renouvelée **à l'expiration** du bail, créant un court trou pendant la
ré-inscription.

Ce fork corrige cela :
- **Bail COV porté de 300 s à 600 s** (`CLIENT_COV_LEASE_SECONDS`) : moins de renouvellements.
- **Renouvellement anticipé « make-before-break »** à **80 %** du bail
  (`CLIENT_COV_RENEW_FRACTION = 0.8`, soit 480 s) : la nouvelle souscription est posée
  **avant** que l'ancienne n'expire, donc plus de coupure ni d'état *indisponible* transitoire.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/client_runtime.py` | **modif** | Bail COV 600 s + constante `CLIENT_COV_RENEW_FRACTION`. |
| `custom_components/bacnet_hub/client_point_entities.py` | **modif** | Renouvellement COV anticipé (make-before-break). |

### Relecture forcée du Present Value après écriture (v1.0.4)

Une écriture BACnet — valeur ou **Null** (relâchement) — ne déclenche pas
systématiquement de notification COV côté automate. Sans relecture, l'entité Home
Assistant restait figée sur la dernière valeur **commandée** jusqu'au cycle COV
suivant : retard mesuré de **plus de 3 minutes** sur un Distech ECB-203 après un
relâchement, l'entité affichant une valeur fausse pendant tout ce temps.

Ce fork ajoute une **relecture du `presentValue`** après chaque écriture et après
chaque relâchement, en **deux passes** (0,5 s puis 3 s, la seconde ne republiant rien
si la valeur n'a pas bougé). La relecture est asynchrone : le service HA rend la main
immédiatement, et un échec de relecture ne fait jamais échouer l'écriture.

**Conséquence :** l'entité affiche désormais la valeur **réelle** de l'automate, et
non la valeur commandée. Si un niveau de priorité plus fort l'emporte, l'entité le
montre au lieu de mentir.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/client_point_entities.py` | **modif** | `_schedule_present_value_refresh()` / `_async_refresh_present_value()`, appelées depuis `_async_write_present_value()` et `_async_release_present_value()`. |

### Noms et unités effacés par un scan partiel (v1.0.4)

À chaque import, l'intégration lit 13 propriétés par objet. Sur une liaison MS/TP
derrière un routeur BACnet, certaines lectures dépassaient le délai et étaient stockées
à `None` — puis le cache du point était **remplacé en bloc**, effaçant un nom ou une
unité pourtant déjà correctement lus. Symptôme : des entités affichées `analog-output 7`
au lieu de `Ventilateur`, et des pourcentages qui perdent leur unité d'un scan à l'autre.

Corrections :

- **Fusion des valeurs non nulles** à l'import (`_merge_non_none`, comme le fait déjà le
  payload device) : une lecture ratée ne peut plus effacer une valeur connue, et les scans
  successifs complètent progressivement les trous.
- **Délai de lecture porté de 2,5 s à 6 s** (`CLIENT_READ_TIMEOUT_SECONDS`), aligné sur
  `CLIENT_POINT_REFRESH_TIMEOUT_SECONDS`, pour réduire les trous à la source.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/sensor.py` | **modif** | Fusion `_merge_non_none` du point relu avec le point en cache. |
| `custom_components/bacnet_hub/client_runtime.py` | **modif** | `CLIENT_READ_TIMEOUT_SECONDS` 2,5 s → 6 s. |

### Écriture en priorité désarmée par une lecture ratée (v1.0.5)

Les points commandables étaient détectés en sondant la propriété `priorityArray`. Une
lecture qui dépassait le délai était stockée à `None`, donc le point était déclaré sans
Priority Array : l'entité `number` redevenait un `sensor`, le bouton Relâcher disparaissait,
et surtout **les écritures partaient sans priorité** — acceptées par l'automate, écrasées
aussitôt, sans aucune erreur.

La priorité d'écriture, le relâchement et la plateforme de l'entité sont désormais décidés
à partir du **type d'objet** (`ao`, `bo`, `av`, `bv`, `mv`, `csv`), qui provient de
l'identifiant de l'objet et ne peut pas se dégrader. Aucune lecture réseau n'intervient
plus dans ce chemin. `has_priority_array` n'est conservé qu'à titre indicatif.

`_protect_point_metadata()` complète `_merge_non_none()` en protégeant aussi les valeurs de
**repli** — nom généré `"<type> <instance>"`, flag `False` — que la fusion laissait passer
puisqu'elles ne sont pas nulles.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/client_runtime.py` | **modif** | `_point_is_writable()` par type, ajout de `_protect_point_metadata()`. |
| `custom_components/bacnet_hub/client_point_entities.py` | **modif** | Priorité et relâchement décidés par type. |
| `custom_components/bacnet_hub/button.py` | **modif** | Bouton Relâcher créé selon le type. |
| `custom_components/bacnet_hub/sensor.py` | **modif** | Protection des métadonnées à l'import. |
| `custom_components/bacnet_hub/__init__.py` | **modif** | Relâchement garanti à l'arrêt de HA et au déchargement. |

### Relâchement garanti à l'arrêt de Home Assistant (v1.0.5)

Une valeur écrite dans un Priority Array y reste jusqu'à ce que quelqu'un écrive `Null`.
Le standard BACnet ne prévoit aucune expiration. Sur un Distech ECB-203, il a été vérifié
que le Priority Array survit à une coupure d'alimentation **et** à 30 minutes de perte de
communication, et que le programme embarqué ne contient aucun chien de garde : l'automate
ne rend jamais la main de lui-même.

Sans relâchement, toute valeur écrite par Home Assistant reste donc inscrite indéfiniment
dès lors que Home Assistant s'arrête, redémarre ou disparaît.

Chaque point réellement commandé est désormais mémorisé, puis relâché :

- à l'arrêt de Home Assistant (`EVENT_HOMEASSISTANT_STOP`) ;
- au déchargement de l'entrée de configuration, avant que la pile BACnet ne s'arrête ;
- l'opération est idempotente et ne relâche que les points effectivement occupés.

Les écritures sont séquentielles — des écritures simultanées se gênent sur une liaison
MS/TP — et encadrées par un budget global : l'arrêt de Home Assistant n'est jamais retardé.
Un point dont le relâchement échoue reste mémorisé pour une tentative ultérieure, et
l'échec est journalisé.

⚠️ Le relâchement rend la main au niveau de priorité inférieur. Si aucun n'est occupé, la
sortie prend la valeur de `Relinquish Default`. Vérifiez cette valeur avant de vous fier au
relâchement comme retour à un état sûr.

Aucune ligne de code de @CervezaStallone n'a été copiée : les deux fichiers ajoutés sont
écrits spécifiquement pour la structure de l'intégration de @magliaral. Seul le **concept**
(un sélecteur de priorité device-level) a servi d'inspiration.

### Démarrage de Home Assistant bloqué par l'import des points (v1.0.6)

Les lectures de points étaient réparties en éventail avec `hass.async_create_task()`, puis
attendues sur place par le `asyncio.gather()` de la ligne suivante. Or Home Assistant
comptabilise les tâches créées par cette API et **les attend à la fin de sa phase de
démarrage**. Sur une liaison lente, quelques dizaines de lectures suffisaient à produire :

```
Setup timed out for bootstrap waiting on Task-…
    _import_client_points.<locals>._read_one() at custom_components/bacnet_hub/sensor.py:278
Something is blocking Home Assistant from wrapping up the start up phase
```

Ces éventails sont internes : ils n'ont aucune raison d'être suivis par Home Assistant.
Ce sont désormais de simples coroutines passées à `asyncio.gather()`. Le helper
`_start_bg_task()`, lui, utilise `hass.async_create_background_task()`, l'API prévue pour
les tâches qui ne doivent pas retenir le démarrage. La branche
`asyncio.run_coroutine_threadsafe` est inchangée : la nouvelle API n'est pas plus
thread-safe que la précédente.

Le parallélisme d'import passe de 8 à 2 et celui de scan de 4 à 2 : sur une liaison MS/TP,
des requêtes simultanées vers un même automate allongent les aller-retours au lieu de les
accélérer.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/sensor.py` | **modif** | Éventails internes en coroutines, `async_create_background_task`, parallélisme réduit. |

### Deux horloges pour une même transaction BACnet (v1.0.6)

L'objet Device local était créé sans fixer `apduTimeout` ni `numberOfApduRetries`.
bacpypes3 appliquait donc ses valeurs de classe — 3000 ms et 3 réessais, soit un budget de
transaction de **12 secondes** — alors que l'intégration enveloppait chaque appel dans des
`asyncio.wait_for()` de 0,6 à 6,0 secondes.

Deux horloges indépendantes pilotaient ainsi la même transaction. Une réponse simplement
lente ne produisait pas un `TimeoutError` propre, mais une trace :

```
bacpypes3/appservice.py:852  in await_confirmation_timeout
bacpypes3/appservice.py:404  in set_state
RuntimeError: invalid state transition from COMPLETED to AWAIT_CONFIRMATION

bacpypes3/app.py:1248  in confirmation
    future.set_result(apdu)
asyncio.exceptions.InvalidStateError: invalid state
```

La machine à états retransmettait pendant qu'une réponse était encore en vol, et la
réponse tardive retombait sur un future déjà résolu.

L'objet Device fixe désormais `apduTimeout = 2000` et `numberOfApduRetries = 1`, soit un
budget de 4,0 s. Les gardes trop courts sont remontés en conséquence : lecture de
l'`objectList` de 0,6 à 4,5 s — 0,6 s était en dessous de tout aller-retour MS/TP
réaliste —, écriture de relâchement de 3,0 à 4,5 s, budget global de relâchement de 15 à
25 s.

> **Invariant à respecter pour toute retouche future :** le budget de transaction
> bacpypes3, `apduTimeout × (1 + numberOfApduRetries)`, doit rester **strictement
> inférieur** au plus court `asyncio.wait_for()` qui enveloppe un service confirmé.
> Aujourd'hui 4,0 s contre 4,5 s.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/server.py` | **modif** | `apduTimeout` et `numberOfApduRetries` explicites sur l'objet Device. |
| `custom_components/bacnet_hub/client_runtime.py` | **modif** | Gardes `wait_for` remontés au-dessus du budget de transaction. |

### Priorité 14 retirée du sélecteur (v1.0.6)

Sur de nombreux automates, la priorité 14 est celle qu'utilise le programme interne.
Un clic de travers dans le menu déroulant suffisait pour que le bouton « Relâcher » y
écrive `Null` et efface la commande native de l'automate. 14 est retirée de
`WRITE_PRIORITY_OPTIONS`.

Effet de bord : un device dont la priorité mémorisée était 14 retombe silencieusement sur
le défaut, 16 — `get_write_priority()` ignore toute valeur hors liste.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/write_priority.py` | **modif** | 14 retirée des niveaux proposés. |

### Tâches de fond encore comptabilisées par Home Assistant (v1.0.7)

La v1.0.6 avait sorti les éventails de lecture de `sensor.py` de la comptabilité de
Home Assistant, mais sept autres appels à `hass.async_create_task()` subsistaient dans
les entités. Le plus gênant :

```python
self._cov_task = self.hass.async_create_task(self._async_cov_receive_loop())

async def _async_cov_receive_loop(self) -> None:
    while True:
        ...
```

Une boucle qui ne se termine jamais, enregistrée parmi les tâches que Home Assistant
attend à la fin de sa phase de démarrage. Relevé dans un journal, sur un démarrage où
l'import des points s'était bien passé :

```
Setup timed out for bootstrap waiting on
    BacnetClientPointEntityBase._async_cov_receive_loop()   × 8
Something is blocking Home Assistant from wrapping up the start up phase
```

Les sept appels passent à `hass.async_create_background_task()`, avec un nom explicite
par tâche.

**L'annulation explicite est conservée.** `async_create_background_task` n'annule qu'à
l'arrêt de Home Assistant ; elle ne couvre ni la suppression d'une entité ni le
déchargement d'une entrée. Le `cancel()` suivi d'un `await`, atteint depuis
`async_will_remove_from_hass`, reste donc en place. L'annulation automatique n'est qu'un
filet de sécurité supplémentaire.

**Écart assumé.** `__init__.py` conserve `hass.async_create_task()` pour la
synchronisation différée des mappings : cette tâche attend un délai d'anti-rebond avant
d'écrire, et une annulation à l'arrêt lui ferait perdre une synchronisation en attente.
Le compromis est inverse de celui des autres tâches.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/client_point_entities.py` | **modif** | 4 tâches passées en arrière-plan : ré-enregistrement COV, boucle de réception COV, renouvellement de bail, relecture du Present Value. |
| `custom_components/bacnet_hub/sensor_entities.py` | **modif** | 3 tâches passées en arrière-plan : ré-enregistrement COV, boucle de réception COV, renouvellement de bail. |

### Lecture du Priority Array à la demande (v1.1.0)

CAS BACnet Explorer affiche les 16 niveaux du Priority Array d'un objet. Home Assistant
n'exposait rien de tel — alors que la donnée transitait déjà. Dans
`_read_client_point_payload`, `priorityArray` figure parmi les propriétés demandées à
l'automate, puis :

```python
has_priority_array = values.get("priorityArray") is not None
```

Les 16 niveaux étaient réduits à un booléen et jetés.

Conserver simplement ce contenu n'aurait pas suffi : cette fonction n'est appelée que
depuis la boucle d'import des points, pas par un rafraîchissement cyclique. La photo
n'arriverait qu'à l'import — rarement, et jamais au moment utile.

**Un bouton « Lire le Priority Array » par device** répond au besoin : à la pression, il
lit les 16 niveaux de chaque point commandable du device et les expose en attributs.

```yaml
last_read: "2026-08-05T09:41:12+00:00"
read_duration_seconds: 4.31
points_count: 5
errors: []
points:
  ao_7:
    object: analog-output,7
    name: Fan
    priorities: [null, null, null, null, null, null, null, 66.0,
                 null, null, null, null, null, 30.0, null, null]
    active_levels: [8, 14]
    highest_active: 8
```

`priorities` est indexé par niveau BACnet : l'élément 1 est la priorité 1.
`active_levels` liste les niveaux occupés, `highest_active` celui qui l'emporte.

**Trois garde-fous :**

*Un niveau à zéro reste un niveau à zéro.* Le cas `null` du choice `PriorityValue`
ressort en `None`, jamais en `0.0` — sans quoi un niveau libre serait indiscernable
d'une commande à zéro, qui est justement l'information recherchée.

*Un échec renvoie `None` franc*, jamais `[]` ni `[None] * 16`. La photo précédente du
point est alors conservée et `errors` signale l'échec. C'est le piège de la v1.0.4 :
`has_priority_array` était un `bool` et `object_name` avait un repli non nul, ce qui les
rendait invisibles à la protection `_merge_non_none`.

*Aucune lecture réseau ne conditionne une écriture.* Le bouton est un outil de
diagnostic ; aucun chemin d'écriture ne l'appelle. C'est l'acquis de la v1.0.5, payé au
prix d'une régression silencieuse en v1.0.4.

**Transport.** La lecture se fait en une requête pour le tableau entier. Si elle échoue,
un repli lit niveau par niveau via `array_index` — pour les automates dont la réponse
complète dépasse `Max Apdu Length Accepted` alors que `Segmentation Supported = None`,
cas qui produit un `Abort from device: 6`. Ce repli coûte 16 aller-retours et ne se
déclenche qu'en dernier recours. Les points sont lus **séquentiellement**, avec 0,2 s
entre chacun : sur une liaison MS/TP, des requêtes simultanées vers un même automate
allongent les aller-retours au lieu de les raccourcir.

**Le bouton est désactivé par défaut**, comme le sélecteur de priorité et le bouton
« Relâcher ». Catégorie diagnostic.

| Fichier | Nature | Description |
| --- | --- | --- |
| `custom_components/bacnet_hub/priority_array_button.py` | **nouveau** | Bouton de lecture, un par device. |
| `custom_components/bacnet_hub/client_runtime.py` | **modif** | Normalisation des `PriorityValue`, lecture avec repli par index. |
| `custom_components/bacnet_hub/button.py` | **modif** | Création du bouton par device. |

### Comportement du bouton « Relâcher » — à connaître

Le bouton écrit `Null` **à la priorité couramment sélectionnée**. Si la priorité change
entre l'écriture et le relâchement, la commande reste figée à l'ancien niveau. Ce n'est
pas un défaut, c'est la conséquence logique du modèle BACnet — mais cela conditionne
toute procédure de reprise de main, et le Priority Array permet désormais de repérer ces
résidus.

---

## 📜 Licence

Ce fork est distribué sous **GNU General Public License v3 (GPL v3)** — voir le fichier
[`LICENSE`](LICENSE).

Raisons de ce choix :
- L'intégration d'origine de @magliaral est sous **MIT**, une licence permissive qui
  **autorise** la redistribution sous une licence plus restrictive comme la GPL v3.
- L'intégration de @CervezaStallone, source d'inspiration, est sous **GPL v3**.
- Publier ce fork en **GPL v3** garantit que **le code reste ouvert et accessible à tous**,
  ainsi que tout dérivé futur — dans le respect de l'esprit open source des deux projets.

La licence MIT d'origine de @magliaral est conservée dans
[`LICENSE_ORIGINAL_magliaral`](LICENSE_ORIGINAL_magliaral) et son copyright reste reconnu.

---

## 🔧 Installation (HACS)

1. HACS → menu ⋮ → **Dépôts personnalisés** → ajouter l'URL de ce dépôt, catégorie
   *Integration*.
2. Installer **BACnet Hub Priority** → redémarrer Home Assistant.
3. Ajouter l'intégration, configurer l'adresse locale (ex. `192.168.1.100/24:47808`).
4. Pour utiliser la priorité : activer l'entité **« Priorité d'écriture »** du device et
   choisir le niveau voulu (ex. **8** pour *Manual Operator*).

---

## ⚠️ Avertissement

Modifier la priorité d'écriture BACnet agit directement sur des équipements CVC/automates.
Une priorité plus haute (valeur plus petite) **prend le pas** sur la régulation interne de
l'automate. À utiliser en connaissance de cause, de préférence après test sur un banc.

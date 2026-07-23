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

Aucune ligne de code de @CervezaStallone n'a été copiée : les deux fichiers ajoutés sont
écrits spécifiquement pour la structure de l'intégration de @magliaral. Seul le **concept**
(un sélecteur de priorité device-level) a servi d'inspiration.

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

# tdash

Dashboard TUI personnalisable (Textual) : perfs, météo (texte ou [weathr](https://github.com/Veirt/weathr)),
lecteur + visualiseur cava, notifications, processus (filtre/kill), Proxmox (API).
Couleurs = celles de ton terminal (ou un thème btop / Textual). Disposition modifiable et enregistrée.

## Installation

Dépendances système (Fedora / Debian-Ubuntu / Arch), puis tdash via pipx :

```bash
# Fedora
sudo dnf install pipx playerctl dunst wireplumber curl cava     # cava est facultatif
# Debian / Ubuntu
sudo apt install pipx playerctl dunst wireplumber curl cava
# Arch / CachyOS
sudo pacman -S python-pipx playerctl dunst wireplumber curl cava

pipx ensurepath
pipx install git+https://github.com/mirtonze/tdash
```

Ou tout d'un coup (détecte apt / dnf / pacman) : `./install.sh` depuis le dépôt cloné,
ou `./install.sh git+https://github.com/<ton-user>/tdash`.

Mise à jour : `pipx upgrade tdash` · Désinstallation : `pipx uninstall tdash`

Python ≥ 3.11 requis (Fedora 39+, Debian 12+, Ubuntu 24.04+, Arch : OK).

## Premier lancement

```bash
tdash --init          # crée ~/.config/dash/config.toml (droits 600) à partir de l'exemple
tdash                 # lance
tdash -p -w           # + Proxmox (-p) et météo animée weathr (-w)
tdash --pve-check     # diagnostic de la connexion Proxmox (token, droits)
```

Options : `-p/--pve`, `-w/--weathr`, `--city`, `--theme`, `--btop-theme`, `--reset-layout`, `--pve-check`, `--init`, `--version`.

## Touches

`?` ouvre l'aide complète. En bref : `1-7` afficher/masquer les panneaux · `e` mode édition
(déplacer/redimensionner, `S` enregistrer) · `/` filtrer les processus · `k` `K` `X` kill (2× pour confirmer) ·
`espace` `n` `p` lecteur · `+` `>` `<` `m` `u` volume · `q` quitter.

## Configuration

- `~/.config/dash/config.toml` : ville, thème, météo, Proxmox, couleurs des panneaux (voir `config.example.toml`).
- `~/.config/dash/layout.json` : disposition enregistrée avec `S`.

Les clés globales (`city`, `theme`…) doivent être **avant** la première `[section]`.

### Proxmox

Token en lecture seule, **avec des droits donnés au token lui-même** (Privilege Separation = oui par défaut) :

```bash
pveum user token add monitor@pve dash --privsep 1
pveum acl modify / --tokens 'monitor@pve!dash' --roles PVEAuditor
```

Le secret (UUID) n'est affiché qu'une fois. Mets-le dans `token_value`, ou dans la variable `PVE_TOKEN`
(qui a priorité). **Ne commite jamais le secret** : si tu versionnes ta config, utilise `PVE_TOKEN`
et mets `token_value` hors du dépôt.

### weathr

`cargo install weathr` (ou le paquet de ta distribution), puis `tdash -w`.
Il tourne dans un terminal intégré d'au moins 70x20 ; les couleurs sont converties vers ta palette.

## Développement

```bash
git clone https://github.com/<ton-user>/tdash && cd tdash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
tdash
```

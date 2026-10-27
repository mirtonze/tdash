#!/usr/bin/env python3
"""tdash — dashboard TUI perso (Textual).

Usage : tdash [-p|--pve] [-w|--weathr] [--city VILLE] [--theme NOM | --btop-theme NOM_OU_FICHIER] [--reset-layout]
Config : ~/.config/dash/config.toml (voir config.example.toml)
Par défaut les couleurs sont celles de ton terminal (palette ANSI).
"""
import argparse
import copy
import fcntl
import json
import locale
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import tomllib
import warnings
from collections import deque
from datetime import datetime
from pathlib import Path

import psutil
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

warnings.filterwarnings("ignore")
try:
    locale.setlocale(locale.LC_TIME, "")
except locale.Error:
    pass

CONFIG: dict = {}
CITY = "Chelles"
BLOCKS = " ▁▂▃▄▅▆▇█"
SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹"
NOTIF_IDS: list = []
HIST: deque = deque(maxlen=400)

# numéro de panneau = position + 1 (comme les boîtes de btop)
ORDER = ["sys", "weather", "notifs", "perf", "player", "procs", "pve"]
TITLES = {
    "sys": "Système", "weather": "Météo", "notifs": "Notifications", "perf": "Perfs",
    "player": "Lecteur", "procs": "Processus", "pve": "Proxmox",
}
# touches numériques : rangée de chiffres en QWERTY, et caractères non shiftés en AZERTY
DIGITS = {
    "1": 1, "&": 1, "2": 2, "é": 2, "3": 3, '"': 3, "4": 4, "'": 4, "5": 5, "(": 5,
    "6": 6, "-": 6, "7": 7, "è": 7, "8": 8, "_": 8, "9": 9, "ç": 9, "0": 0, "à": 0,
}

# couleur de bordure/titre de chaque panneau (noms ANSI = palette de ton terminal, ou #rrggbb)
# modifiable dans config.toml, section [colors]
DEFAULT_COLORS = {
    "sys": "cyan", "weather": "bright_blue", "notifs": "magenta", "perf": "blue",
    "player": "green", "procs": "red", "pve": "yellow", "help": "bright_white",
}
# avec --theme NOM, on prend les couleurs du thème (rôles) plutôt que celles du terminal
ROLE_OF = {"blue": "primary", "cyan": "secondary", "bright_blue": "accent", "magenta": "accent",
           "green": "success", "yellow": "warning", "red": "error"}
ANSI_NAMES = {"black", "red", "green", "yellow", "blue", "magenta", "cyan", "white", "bright_black", "bright_red",
              "bright_green", "bright_yellow", "bright_blue", "bright_magenta", "bright_cyan", "bright_white"}


def to_color(c: str) -> str:
    c = str(c).strip()
    if c.startswith(("#", "ansi_")):
        return c
    return "ansi_" + c if c in ANSI_NAMES else c


CAVA_CONF = """[general]
bars = 48
framerate = 30
[input]
method = pulse
source = auto
[output]
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = 8
bar_delimiter = 59
frame_delimiter = 10
"""


def T(pid: str) -> str:
    return f"{SUP[ORDER.index(pid) + 1]} {TITLES[pid]}"


# ───────────── helpers ─────────────
def sh(cmd: str, timeout: int = 4) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def run(cmd: str) -> None:
    subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def bar(pct: float, width: int) -> str:
    width = max(width, 1)
    n = round(width * max(0.0, min(pct, 100.0)) / 100)
    return "█" * n + "░" * (width - n)


def spark(values, width: int) -> str:
    vals = list(values)[-width:]
    s = "".join(BLOCKS[min(8, int(v / 100 * 8.99))] for v in vals)
    return s.rjust(width)


def cut(s: str, w: int) -> str:
    return s if len(s) <= w else s[: max(w - 1, 0)] + "…"


def human(n: float) -> str:
    for u in ("o/s", "Ko/s", "Mo/s"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} Go/s"


def fmt_time(s: float) -> str:
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def load_config() -> dict:
    try:
        cfg = tomllib.loads((Path.home() / ".config/dash/config.toml").read_text())
    except Exception:
        return {}
    # en TOML, une clé écrite après [pve] appartient à [pve] : on la remonte au niveau global
    pve = cfg.get("pve")
    if isinstance(pve, dict):
        for k in ("city", "theme", "btop_theme", "weather_mode", "weathr_cmd", "weathr_colors"):
            if k in pve and k not in cfg:
                cfg[k] = pve.pop(k)
    return cfg


def parse_btop_color(v: str):
    v = v.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 2:
            h *= 3
        if len(h) == 6 and re.fullmatch(r"[0-9a-fA-F]{6}", h):
            return "#" + h.lower()
        return None
    parts = v.split()
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return "#%02x%02x%02x" % tuple(min(int(p), 255) for p in parts)
    return None


def load_btop_theme(name: str) -> dict:
    """Lit un thème btop (fichier .theme) : chemin, ~/.config/btop/themes ou /usr/share/btop/themes."""
    candidates = [
        Path(name).expanduser(),
        Path.home() / ".config/btop/themes" / f"{name}.theme",
        Path("/usr/share/btop/themes") / f"{name}.theme",
        Path("/usr/local/share/btop/themes") / f"{name}.theme",
    ]
    for p in candidates:
        if p.is_file():
            colors = {}
            for line in p.read_text(errors="ignore").splitlines():
                m = re.match(r'\s*theme\[(\w+)\]\s*=\s*"(.*?)"', line)
                if m:
                    c = parse_btop_color(m.group(2))
                    if c:
                        colors[m.group(1)] = c
            return colors
    return {}


# ───────────── sources de données (renvoient str ou Text) ─────────────
def get_volume():
    out = sh("wpctl get-volume @DEFAULT_AUDIO_SINK@")  # "Volume: 0.45" ou "... [MUTED]"
    try:
        return float(out.split()[1]) * 100, "MUTED" in out
    except Exception:
        return None


def sysinfo(w: int, h: int):
    now = datetime.now()
    bw = max(w - 14, 8)
    lines = [now.strftime("%H:%M:%S"), now.strftime("%A %d %B %Y").capitalize()]
    b = psutil.sensors_battery()
    if b:
        lines.append(f"BAT {bar(b.percent, bw)} {b.percent:3.0f}%{' ⚡' if b.power_plugged else ''}")
    v = get_volume()
    if v:
        lines.append(f"VOL {bar(min(v[0], 100), bw)} {v[0]:3.0f}%{' muet' if v[1] else ''}")
    return "\n".join(cut(line, w) for line in lines[:h])


def weather(w: int, h: int):
    a = sh(f"curl -s -m 3 'wttr.in/{CITY}?format=3'")
    b = sh(f"curl -s -m 3 'wttr.in/{CITY}?format=%C,+ressenti+%f,+vent+%w,+hum.+%h&lang=fr'")
    if not a:
        return "météo indisponible"
    return "\n".join(cut(line, w) for line in (a, b) if line)[:2000]


_net = None


def net_rates():
    global _net
    c = psutil.net_io_counters()
    now = time.time()
    prev, _net = _net, (now, c.bytes_sent, c.bytes_recv)
    if not prev:
        return 0.0, 0.0
    dt = max(now - prev[0], 1e-6)
    return (c.bytes_sent - prev[1]) / dt, (c.bytes_recv - prev[2]) / dt


def temperature():
    try:
        t = psutil.sensors_temperatures()
        for k in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            if t.get(k):
                return t[k][0].current
    except Exception:
        pass
    return None


def perf(w: int, h: int):
    cores = psutil.cpu_percent(percpu=True)
    cpu = sum(cores) / len(cores)
    HIST.append(cpu)
    vm, sw, du = psutil.virtual_memory(), psutil.swap_memory(), psutil.disk_usage("/")
    up, down = net_rates()
    t = temperature()
    bw = max(w - 32, 8)
    lines = [
        f"CPU  {cpu:5.1f}% {spark(HIST, max(w - 12, 5))}",
        "Cœurs " + "".join(BLOCKS[min(8, int(p / 100 * 8.99))] for p in cores),
        f"RAM  {bar(vm.percent, bw)} {vm.percent:4.1f}% {vm.used / 2**30:.1f}/{vm.total / 2**30:.1f}G",
        f"Swap {bar(sw.percent, bw)} {sw.percent:4.1f}%",
        f"Disq {bar(du.percent, bw)} {du.percent:4.1f}%",
        (f"Temp {t:.0f}°C   " if t is not None else "") + f"Réseau ↑{human(up)} ↓{human(down)}",
    ]
    return "\n".join(cut(line, w) for line in lines[: max(h, 1)])


def player(w: int, h: int):
    out = sh("playerctl metadata --format '{{status}}|{{artist}}|{{title}}|{{album}}|{{position}}|{{mpris:length}}'")
    if not out or out.count("|") < 5:
        return "rien en lecture"
    head, pos, length = out.rsplit("|", 2)
    status, artist, title, album = (head.split("|", 3) + ["", "", "", ""])[:4]
    icon = {"Playing": "⏸", "Paused": "▶"}.get(status, "■")
    lines = [f"{icon} {title}", f"{artist} — {album}" if album else artist]
    try:
        p, ln = int(pos) / 1e6, int(length) / 1e6
        bw = max(w - 14, 8)
        k = min(bw - 1, int(bw * p / ln)) if ln else 0
        lines.append("━" * k + "●" + "─" * (bw - k - 1) + f" {fmt_time(p)}/{fmt_time(ln)}")
    except Exception:
        pass
    return "\n".join(cut(line, w) for line in lines[:h])


def notifs(w: int, h: int):
    try:
        items = json.loads(sh("dunstctl history"))["data"][0]
    except Exception:
        NOTIF_IDS.clear()
        return "dunstctl indisponible"
    NOTIF_IDS[:] = [n["id"]["data"] for n in items]
    if not items:
        return "aucune notification"
    return "\n".join(cut(f"• {n['appname']['data']}: {n['summary']['data']}", w) for n in items[:h])


_px = None


def pve_settings():
    """Lit [pve] du config.toml en corrigeant les erreurs courantes. Renvoie (réglages, avertissements)."""
    cfg = CONFIG.get("pve", {})
    warns = []
    host = re.sub(r"^https?://", "", str(cfg.get("host", "")).strip()).split("/")[0]
    user = str(cfg.get("user", "")).strip()
    tname = str(cfg.get("token_name", "")).strip()
    if "!" in tname:  # "monitor@pve!dash" collé dans token_name
        u, _, tname = tname.partition("!")
        user = user or u
        warns.append("token_name contenait « user@realm!id » : seul l'ID (après le !) est utilisé")
    if "!" in user:  # "monitor@pve!dash" collé dans user
        user, _, t = user.partition("!")
        tname = tname or t
        warns.append("user contenait « !id » : séparé en user et token_name")
    env = os.environ.get("PVE_TOKEN", "").strip().strip("\"'")
    cfgtok = str(cfg.get("token_value", "")).strip().strip("\"'")
    token = env or cfgtok
    if env and cfgtok and env != cfgtok:
        warns.append("PVE_TOKEN (shell) diffère de token_value (config.toml) : c'est PVE_TOKEN qui est utilisé")
    if token and not re.fullmatch(r"[0-9a-fA-F-]{36}", token):
        warns.append("le secret ne ressemble pas à un UUID de 36 caractères (copie complète ? guillemets ?)")
    return {
        "host": host, "user": user, "token_name": tname, "token": token,
        "verify_ssl": cfg.get("verify_ssl", False),
        "source": "variable PVE_TOKEN" if env else "config.toml",
    }, warns


def pve_connect():
    s, _ = pve_settings()
    missing = [k for k, v in (("host", s["host"]), ("user", s["user"]), ("token_name", s["token_name"]),
                              ("secret", s["token"])) if not v]
    if missing:
        raise ValueError("config [pve] incomplète : " + ", ".join(missing) + " manquant")
    import urllib3
    from proxmoxer import ProxmoxAPI

    urllib3.disable_warnings()
    return ProxmoxAPI(s["host"], user=s["user"], token_name=s["token_name"],
                      token_value=s["token"], verify_ssl=s["verify_ssl"])


def pve_error(e: Exception) -> str:
    t = str(e)
    if "401" in t:
        return "Proxmox : authentification refusée (user, token_name ou secret) — lance ttdash --pve-check"
    return f"Proxmox indisponible : {t} — lance ttdash --pve-check"


def pve_check(px=None) -> None:
    """Diagnostic pas à pas de la connexion Proxmox (ttdash --pve-check)."""
    s, warns = pve_settings()
    cfgfile = Path.home() / ".config/dash/config.toml"
    tok = s["token"]
    masked = f"{tok[:4]}…{tok[-4:]} ({len(tok)} car.)" if len(tok) > 8 else ("vide" if not tok else "trop court")
    print("== Vérification Proxmox ==")
    print(f"config : {cfgfile} ({'présent' if cfgfile.exists() else 'ABSENT'})")
    print(f"host={s['host'] or 'VIDE'}  user={s['user'] or 'VIDE'}  token_name={s['token_name'] or 'VIDE'}")
    print(f"secret={masked}  source : {s['source']}  verify_ssl={s['verify_ssl']}")
    for w in warns:
        print("⚠", w)
    if px is None:
        try:
            px = pve_connect()
        except Exception as e:
            print("✘", e)
            return
    try:
        v = px.version.get()
        print(f"✔ connexion et authentification OK (Proxmox {v.get('version', '?')})")
    except Exception as e:
        print(f"✘ connexion/authentification : {e}")
        print("  → host : IP ou nom seul (ex 192.168.1.10), user (ex monitor@pve), token_name : l'ID seul (ex dash),")
        print("    secret : l'UUID affiché UNE SEULE FOIS à la création du token (sinon recrée le token)")
        return
    perms = None
    try:
        perms = px.access.permissions.get()
        if perms:
            for path in sorted(perms):
                p = perms[path]
                tags = [t for t in ("Sys.Audit", "VM.Audit") if t in p]
                print(f"  droits sur {path} : {len(p)} privilèges {tags}")
        else:
            print("✘ ce token n'a AUCUN droit")
    except Exception as e:
        print("⚠ permissions illisibles :", e)
    has_sys = bool(perms) and any("Sys.Audit" in p for p in perms.values())
    has_vm = bool(perms) and any("VM.Audit" in p for p in perms.values())
    try:
        nodes = px.nodes.get()
        res = px.cluster.resources.get(type="vm")
        seen = sum(1 for n in nodes if n.get("maxmem"))
        print(f"  l'API montre {len(nodes)} nœud(s) dont {seen} avec CPU/RAM, et {len(res)} VM/CT")
    except Exception as e:
        print("⚠ lecture des nœuds/VM :", e)
    if has_sys and has_vm:
        print("✔ droits suffisants : le panneau doit afficher nœuds et VM")
    else:
        print("✘ droits de lecture manquants (Sys.Audit et/ou VM.Audit). À lancer sur le nœud Proxmox :")
        print(f"    pveum user token list {s['user']}      # privsep = 1 : le token n'hérite PAS des droits de l'utilisateur")
        print(f"    pveum acl modify / --tokens '{s['user']}!{s['token_name']}' --roles PVEAuditor")


def _gb(n) -> str:
    return f"{n / 2**30:.1f}"


def pve_status(w: int, h: int):
    global _px
    try:
        if _px is None:
            _px = pve_connect()
        nodes = _px.nodes.get()
        res = _px.cluster.resources.get(type="vm")
    except Exception as e:
        _px = None
        return cut(pve_error(e), w * 3)
    vms = [v for v in res if not v.get("template")]
    if not vms:  # repli : interroger chaque nœud
        for n in nodes:
            for kind in ("qemu", "lxc"):
                try:
                    for v in getattr(_px.nodes(n["node"]), kind).get():
                        v = dict(v, type=kind, node=n["node"])
                        if not v.get("template"):
                            vms.append(v)
                except Exception:
                    pass
    lines = []
    # le token voit-il les métriques ? (sans droit Sys.Audit / VM.Audit, l'API omet les champs)
    blind_nodes = [n for n in nodes if not n.get("maxmem")]
    if blind_nodes:
        lines.append(Text("⚠ token sans droits de lecture : CPU/RAM/VM masqués (voir pveum acl)", style="yellow"))
    for n in nodes:
        ok = n.get("status") == "online"
        t = Text()
        t.append("● " if ok else "○ ", style="green" if ok else "red")
        t.append(str(n["node"]))
        if n.get("maxmem"):
            t.append(f"   CPU {n.get('cpu', 0) * 100:.0f}%   RAM {_gb(n.get('mem', 0))}/{_gb(n['maxmem'])}G")
            if n.get("uptime"):
                t.append(f"   up {n['uptime'] // 86400}j")
        else:
            t.append("   CPU/RAM : n/d")
        lines.append(t)
    if not vms:
        lines.append(Text("aucune VM/CT visible" + (" (droits VM.Audit ?)" if blind_nodes else ""), style="dim"))
    nw = max(w - 36, 10)
    for v in sorted(vms, key=lambda v: (v.get("status") != "running", v.get("vmid", 0))):
        up = v.get("status") == "running"
        kind = "CT" if v.get("type") == "lxc" else "VM"
        t = Text()
        t.append("  ● " if up else "  ○ ", style="green" if up else "red")
        t.append(cut(f"{v.get('vmid', '?')} {v.get('name', '?')}", nw).ljust(nw) + f" {kind} ")
        if up and v.get("maxmem"):
            t.append(f"CPU {v.get('cpu', 0) * 100:3.0f}%  RAM {_gb(v.get('mem', 0))}/{_gb(v['maxmem'])}G")
        elif not up:
            t.append("arrêtée", style="dim")
        lines.append(t)
    return Text("\n").join(lines[: max(h, 1)])


# ───────────── widgets ─────────────
class Panel(Static):
    def __init__(self, title: str, fn, every: float, **kw):
        super().__init__(**kw)
        self.border_title = title
        self.fn = fn
        self.every = every

    def on_mount(self) -> None:
        self.set_interval(self.every, self.refresh_data)
        self.refresh_data()

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        w, h = self.content_size.width, self.content_size.height
        if w <= 0 or h <= 0:  # panneau masqué ou pas encore dimensionné
            return
        self.run_worker(
            lambda: self.collect(w, h), thread=True, exclusive=True, group=f"p-{self.id}", exit_on_error=False
        )

    def collect(self, w: int, h: int) -> None:
        try:
            r = self.fn(w, h)
        except Exception as e:
            r = f"erreur : {e}"
        if isinstance(r, str):
            r = Text(r)
        self.app.call_from_thread(self.update, r)


class Media(Static):
    """Lecteur (titre, artiste, progression) avec le visualiseur cava juste en dessous."""

    def __init__(self, title: str, **kw):
        super().__init__(**kw)
        self.border_title = title
        self.levels: list = []
        self.info: list = ["rien en lecture"]
        self.proc = None
        self.cfg = None
        self.last = ""

    def on_mount(self) -> None:
        self.has_cava = shutil.which("cava") is not None
        if self.has_cava:
            threading.Thread(target=self.reader, daemon=True).start()
        self.set_interval(1 / 20, self.draw)
        self.set_interval(1, self.refresh_data)
        self.refresh_data()

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        w = self.content_size.width
        if w <= 0:
            return
        self.run_worker(lambda: self.fetch(w), thread=True, exclusive=True, group="media", exit_on_error=False)

    def fetch(self, w: int) -> None:
        try:
            self.info = player(w, 3).split("\n")
        except Exception as e:
            self.info = [f"erreur : {e}"]

    def reader(self) -> None:
        f = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
        f.write(CAVA_CONF)
        f.close()
        self.cfg = f.name
        self.proc = subprocess.Popen(
            ["cava", "-p", f.name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        for line in self.proc.stdout:
            vals = [int(v) for v in line.strip().split(";") if v.isdigit()]
            if vals:
                self.levels = vals

    def draw(self) -> None:
        w, h = self.content_size.width, self.content_size.height
        if w <= 0 or h <= 0:
            return
        info = [cut(line, w) for line in self.info[: max(h - 1, 1)]]
        bh = h - len(info)  # le reste de la hauteur : barres cava
        rows = []
        if bh >= 1:
            if not self.has_cava:
                rows = [cut("cava non installé (sudo apt install cava)", w)]
            elif self.levels:
                lv = self.levels
                cols = [lv[min(len(lv) - 1, i * len(lv) // w)] for i in range(w)]
                for k in range(bh - 1, -1, -1):
                    rows.append("".join(BLOCKS[max(0, min(8, round(v / 8 * bh * 8) - k * 8))] for v in cols))
        out = "\n".join(info + rows)
        if out != self.last:
            self.last = out
            self.update(Text(out))

    def on_unmount(self) -> None:
        if self.proc:
            self.proc.terminate()
        if self.cfg:
            try:
                os.unlink(self.cfg)
            except OSError:
                pass


class Procs(DataTable):
    def __init__(self, title: str, **kw):
        super().__init__(**kw)
        self.base_title = title
        self.sort_key = "cpu_percent"
        self.filter_text = ""
        self.procs: list = []    # (pid, nom) affichés
        self.matches: list = []  # tous les pid qui correspondent au filtre

    def on_mount(self) -> None:
        self.update_title()
        self.cursor_type = "row"
        self.add_columns("PID", "Nom", "CPU%", "MEM%")
        self.set_interval(2, self.refresh_data)

    def update_title(self) -> None:
        self.border_title = self.base_title + (f"   /{self.filter_text}" if self.filter_text else "")

    def set_filter(self, text: str) -> None:
        self.filter_text = text.strip().lower()
        self.update_title()
        self.refresh_data()

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        h = self.content_size.height
        if h <= 1:
            return
        n, key, q = h - 1, self.sort_key, self.filter_text
        self.run_worker(
            lambda: self.collect(n, key, q), thread=True, exclusive=True, group="procs", exit_on_error=False
        )

    def collect(self, n: int, key: str, q: str) -> None:
        rows = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            i = p.info
            if q and q not in (i["name"] or "").lower() and q != str(i["pid"]):
                continue
            rows.append(i)
        rows.sort(key=lambda i: i[key] or 0, reverse=True)
        self.app.call_from_thread(self.populate, rows[:n], [r["pid"] for r in rows])

    def populate(self, rows, all_pids) -> None:
        cur = self.cursor_row
        self.clear()
        self.procs = []
        self.matches = all_pids
        for i in rows:
            name = (i["name"] or "?")[:22]
            self.add_row(str(i["pid"]), name, f"{i['cpu_percent'] or 0:5.1f}", f"{i['memory_percent'] or 0:5.1f}")
            self.procs.append((i["pid"], name))
        if self.row_count:
            self.move_cursor(row=min(cur, self.row_count - 1))



# ───────────── terminal intégré (pour weathr) ─────────────
_PYTE_NAMES = {"brown": "yellow", "brightbrown": "bright_yellow"}
_style_cache: dict = {}


# weathr -> palette du terminal. weathr n'écrit que des couleurs de texte ; ses gris foncés (DarkGrey)
# disparaissent sur un fond sombre, on les remplace par des couleurs lisibles de la palette.
WEATHR_NAMES = {
    "black": "white", "red": "red", "green": "green", "brown": "yellow", "blue": "bright_blue",
    "magenta": "red", "cyan": "cyan", "white": "white",
    "brightblack": "white", "brightred": "bright_red", "brightgreen": "bright_green",
    "brightbrown": "bright_yellow", "brightblue": "bright_blue", "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan", "brightwhite": "bright_white",
}


def rgb_to_palette(hexc: str, is_bg: bool = False):
    """RVB -> couleur ANSI lisible, par teinte (gris -> blanc, brun/orange -> jaune, bleu toujours clair)."""
    import colorsys

    r, g, b = int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if is_bg and v < 0.35:
        return None  # fond sombre = fond du terminal
    if s < 0.18:  # gris
        return "bright_white" if v > 0.85 else "white"
    deg = h * 360
    if deg >= 345 or deg < 15:
        name = "red"
    elif deg < 70:
        name = "yellow"
    elif deg < 170:
        name = "green"
    elif deg < 200:
        name = "cyan"
    elif deg < 260:
        name = "blue"
    else:
        name = "magenta"
    return "bright_" + name if (v > 0.8 or name == "blue") else name


def pyte_color(c: str, quantize: bool = False, is_bg: bool = False):
    if not c or c == "default":
        return None
    if re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return rgb_to_palette(c, is_bg) if quantize else "#" + c
    if quantize:
        return None if (is_bg and c == "black") else WEATHR_NAMES.get(c, c)
    c = _PYTE_NAMES.get(c, c)
    return "bright_" + c[6:] if c.startswith("bright") else c


def pyte_style(ch, quantize: bool = False):
    key = (ch.fg, ch.bg, ch.bold, ch.italics, ch.reverse, ch.underscore, quantize)
    st = _style_cache.get(key)
    if st is None:
        from rich.style import Style

        try:
            st = Style(color=pyte_color(ch.fg, quantize), bgcolor=pyte_color(ch.bg, quantize, True), bold=ch.bold,
                       italic=ch.italics, reverse=ch.reverse, underline=ch.underscore)
        except Exception:
            st = Style()
        _style_cache[key] = st
    return st


MIN_TERM = (70, 20)  # weathr refuse de tourner en dessous


class Term(Static):
    """Fait tourner un programme plein écran (ex. weathr) dans un pty et affiche son écran (émulation pyte).
    Affichage seul : les touches ne sont pas transmises au programme."""

    def __init__(self, title: str, cmd: str, colors: str = "terminal", **kw):
        super().__init__(**kw)
        self.border_title = title
        self.cmd = cmd
        self.quantize = colors != "truecolor"  # défaut : couleurs du terminal
        self.proc = None
        self.fd = None
        self.term = None
        self.size_ = (0, 0)
        self.lock = threading.Lock()
        self.version = 0
        self.drawn = -1
        self.view = (0, 0)  # taille visible du panneau
        self.error = ""

    def on_mount(self) -> None:
        self.set_interval(1 / 12, self.draw)

    def on_resize(self, event: events.Resize) -> None:
        w, h = self.content_size.width, self.content_size.height
        if w < 10 or h < 4:
            self.stop()
            return
        self.view = (w, h)
        self.drawn = -1
        vw, vh = max(w, MIN_TERM[0]), max(h, MIN_TERM[1])  # le programme voit au moins 70x20
        if self.proc is None or self.proc.poll() is not None:
            self.start(vw, vh)
        elif (vw, vh) != self.size_:
            self.resize(vw, vh)

    def start(self, w: int, h: int) -> None:
        import pty

        import pyte

        self.stop()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", h, w, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")  # sinon weathr passe tout en blanc
        def child():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.proc = subprocess.Popen(
                shlex.split(self.cmd), stdin=slave, stdout=slave, stderr=slave,
                env=env, preexec_fn=child, close_fds=True,
            )
        except Exception as e:
            os.close(master)
            os.close(slave)
            self.error = f"erreur : {e}"
            return
        os.close(slave)
        self.fd, self.size_ = master, (w, h)
        self.error = ""
        term = pyte.Screen(w, h)
        term.write_process_input = lambda data, fd=master: self.reply(fd, data)
        self.term = term
        threading.Thread(target=self.pump, args=(master, pyte.ByteStream(term)), daemon=True).start()

    @staticmethod
    def reply(fd: int, data: str) -> None:
        try:
            os.write(fd, data.encode())
        except OSError:
            pass

    def pump(self, fd: int, stream) -> None:
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                stream.feed(data)
                self.version += 1

    def resize(self, w: int, h: int) -> None:
        self.size_ = (w, h)
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", h, w, 0, 0))
        except OSError:
            pass
        with self.lock:
            self.term.resize(h, w)
            self.version += 1

    def stop(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                threading.Thread(target=self.proc.wait, daemon=True).start()
            self.proc = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def restart(self) -> None:
        w, h = self.view
        if w >= 10 and h >= 4:
            self.start(max(w, MIN_TERM[0]), max(h, MIN_TERM[1]))

    def draw(self) -> None:
        w, h = self.view
        if self.error:
            hint = self.error
        elif self.proc is not None and self.proc.poll() is not None:
            hint = f"arrêté (code {self.proc.returncode}) — w pour relancer"
        elif w and (w < MIN_TERM[0] or h < MIN_TERM[1]):
            hint = f"affichage rogné : {MIN_TERM[0]}x{MIN_TERM[1]} min, agrandis le panneau"
        else:
            hint = ""
        if self.border_subtitle != hint:
            self.border_subtitle = hint
        if self.term is None:
            return
        with self.lock:
            if self.version == self.drawn:
                return
            self.drawn = self.version
            cw, ch = min(w, self.size_[0]), min(h, self.size_[1])
            out = Text(no_wrap=True)
            for y in range(ch):
                row = self.term.buffer[y]
                run, style = "", None
                for x in range(cw):
                    c = row[x]
                    st = pyte_style(c, self.quantize)
                    if run and st != style:
                        out.append(run, style)
                        run = ""
                    style = st
                    run += c.data or " "
                out.append(run, style)
                if y < ch - 1:
                    out.append("\n")
        self.update(out)

    def on_unmount(self) -> None:
        self.stop()


# ───────────── disposition : colonnes de panneaux empilés ─────────────
LAYOUT_FILE = Path.home() / ".config/dash/layout.json"
DEFAULT_SPEC = {"sys": "6", "weather": "4", "notifs": "1fr", "perf": "10",
                "player": "13", "procs": "1fr", "pve": "1fr"}
SPEC_RE = re.compile(r"^(\d+|\d+(\.\d+)?fr)$")
MIN_H, MIN_W = 3, 8


def make_defaults(weathr: bool, pve: bool = False) -> dict:
    side = [["sys", "6"], ["player", "13"], ["weather", "4"], ["notifs", "1fr"]]
    d = {
        "narrow": {"cols": [{"w": 1, "panels": [["sys", "6"], ["perf", "1fr"], ["player", "12"]]}],
                   "hidden": ["weather", "notifs", "procs", "pve"]},
        "medium": {"cols": [{"w": 3, "panels": [["perf", "10"], ["procs", "1fr"], ["pve", "1fr"]]},
                            {"w": 2, "panels": copy.deepcopy(side)}],
                   "hidden": []},
        "wide": {"cols": [{"w": 2, "panels": [["perf", "10"], ["procs", "1fr"]]},
                          {"w": 1, "panels": copy.deepcopy(side)},
                          {"w": 1, "panels": [["pve", "1fr"]]}],
                 "hidden": []},
    }
    if weathr:  # weathr veut au moins 70x20 : on lui réserve une grande colonne
        small = [["sys", "6"], ["player", "13"], ["notifs", "1fr"]]
        d["medium"] = {"cols": [{"w": 2, "panels": [["weather", "1fr"], ["perf", "10"]]},
                                {"w": 1, "panels": small + [["pve", "1fr"]]}],
                       "hidden": ["procs"]}
        d["wide"] = {"cols": [{"w": 2, "panels": [["perf", "10"], ["procs", "1fr"]]},
                              {"w": 4, "panels": [["weather", "22" if pve else "1fr"], ["pve", "1fr"]]},
                              {"w": 2, "panels": copy.deepcopy(small)}],
                     "hidden": []}
    return d


def prune_layout(lay, ids):
    """Valide une disposition : retire les panneaux inconnus, ajoute ceux qui manquent."""
    try:
        cols, seen = [], set()
        for c in lay["cols"]:
            ps = []
            for pid, spec in c["panels"]:
                if pid in ids and pid not in seen and SPEC_RE.match(str(spec)):
                    ps.append([pid, str(spec)])
                    seen.add(pid)
            if ps:
                cols.append({"w": max(1, min(9, int(c["w"]))), "panels": ps})
        if not cols:
            cols = [{"w": 1, "panels": []}]
        for pid in ids:
            if pid not in seen:
                cols[-1]["panels"].append([pid, DEFAULT_SPEC[pid]])
        return {"cols": cols, "hidden": [p for p in lay.get("hidden", []) if p in ids]}
    except Exception:
        return None


def load_saved() -> dict:
    try:
        d = json.loads(LAYOUT_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_saved(d: dict) -> None:
    if not d:
        LAYOUT_FILE.unlink(missing_ok=True)
        return
    LAYOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAYOUT_FILE.write_text(json.dumps(d, indent=1))


def is_fr(spec: str) -> bool:
    return spec.endswith("fr")


def compute_rects(lay: dict, visible: set, W: int, H: int) -> dict:
    """Place les panneaux : colonnes proportionnelles, hauteurs fixes (lignes) ou flexibles (fr)."""
    cols = []
    for c in lay["cols"]:
        ps = [(pid, spec) for pid, spec in c["panels"] if pid in visible]
        if ps:
            cols.append((c["w"], ps))
    rects: dict = {}
    if not cols or W <= 0 or H <= 0:
        return rects
    total, acc, x = sum(w for w, _ in cols), 0, 0
    for ci, (cw, ps) in enumerate(cols):
        acc += cw
        x2 = W if ci == len(cols) - 1 else round(W * acc / total)
        fixed = sum(max(int(s), MIN_H) for _, s in ps if not is_fr(s))
        frs = sum(float(s[:-2]) for _, s in ps if is_fr(s))
        avail, y, done, accfr = max(H - fixed, 0), 0, 0, 0.0
        for pid, s in ps:
            if is_fr(s):
                accfr += float(s[:-2])
                end = round(avail * accfr / frs)
                hh = max(end - done, MIN_H)
                done = end
            else:
                hh = max(int(s), MIN_H)
            rects[pid] = (x, y, x2 - x, hh)
            y += hh
        x = x2
    return rects


def find_panel(lay: dict, pid: str):
    for ci, c in enumerate(lay["cols"]):
        for pi, (p, _) in enumerate(c["panels"]):
            if p == pid:
                return ci, pi
    return None


def move_v(lay: dict, pid: str, d: int, hidden) -> bool:
    ci, pi = find_panel(lay, pid)
    ps = lay["cols"][ci]["panels"]
    j = pi + d
    while 0 <= j < len(ps) and ps[j][0] in hidden:
        j += d
    if 0 <= j < len(ps):
        ps[pi], ps[j] = ps[j], ps[pi]
        return True
    return False


def move_h(lay: dict, pid: str, d: int) -> bool:
    ci, pi = find_panel(lay, pid)
    cols = lay["cols"]
    entry = cols[ci]["panels"].pop(pi)
    tc = ci + d
    if 0 <= tc < len(cols):
        tp = cols[tc]["panels"]
        tp.insert(min(pi, len(tp)), entry)
    elif cols[ci]["panels"]:  # au bord : crée une nouvelle colonne
        cols.insert(0 if d < 0 else len(cols), {"w": 1, "panels": [entry]})
    else:
        cols[ci]["panels"].append(entry)
        return False
    lay["cols"] = [c for c in cols if c["panels"]]
    return True


def resize_w(lay: dict, pid: str, d: int) -> None:
    ci, _ = find_panel(lay, pid)
    c = lay["cols"][ci]
    c["w"] = max(1, min(9, c["w"] + d))


def resize_h(lay: dict, pid: str, d: int) -> None:
    ci, pi = find_panel(lay, pid)
    e = lay["cols"][ci]["panels"][pi]
    if is_fr(e[1]):
        e[1] = f"{max(1, min(9, int(float(e[1][:-2])) + d))}fr"
    else:
        e[1] = str(max(MIN_H, min(60, int(e[1]) + d)))


def toggle_unit(lay: dict, pid: str, cur_h: int) -> None:
    ci, pi = find_panel(lay, pid)
    e = lay["cols"][ci]["panels"][pi]
    e[1] = "1fr" if not is_fr(e[1]) else str(max(MIN_H, cur_h or 6))


# ───────────── aide (popup) ─────────────
def build_help(weathr: bool) -> Text:
    rows = [
        ("Panneaux", None),
        ("1-7", "afficher / masquer un panneau (numéro dans le titre) · 0 = défaut"),
        ("Lecteur + cava", None),
        ("espace", "pause · n suivant · p précédent" + (" · w relancer weathr" if weathr else "")),
        ("Volume", None),
        ("+  >  <", "monter · monter · baisser"),
        ("m  u", "couper / remettre le son · démuter"),
        ("Processus", None),
        ("/  f", "filtrer (Entrée valide, Échap efface) · ↑ ↓ choisir"),
        ("c  r", "trier par CPU · par RAM"),
        ("k  K  X", "SIGTERM · SIGKILL · tout le filtre (2× pour confirmer)"),
        ("Notifications", None),
        ("d  D", "effacer la dernière · tout effacer"),
        ("Disposition", None),
        ("e", "mode édition (Échap pour sortir) · S enregistrer"),
        ("← →", "déplacer le panneau vers la colonne voisine"),
        ("↑ ↓", "monter / descendre dans la colonne"),
        ("⇧ + flèches", "ou a d (largeur) · w s (hauteur)"),
        ("t", "hauteur fixe (lignes) ↔ flexible (fr) · Tab panneau suivant"),
        ("R", "disposition par défaut"),
        ("?  q", "cette aide · quitter"),
    ]
    out = Text()
    for k, v in rows:
        if v is None:
            if out.plain:
                out.append("\n")
            out.append(k, style="bold underline")
            out.append("\n")
        else:
            out.append(f"  {k:<12}", style="bold")
            out.append(v + "\n")
    out.rstrip()
    return out


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark", "close", "Fermer")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: transparent; }
    HelpScreen #help { width: 84; max-width: 98%; height: auto; max-height: 98%;
                       border: round $primary; padding: 0 1; overflow-y: auto; }
    """

    def __init__(self, content: Text, color: str | None = None, fg: str | None = None, bg: str | None = None):
        super().__init__()
        self.content, self.color, self.fg, self.bg = content, color, fg, bg

    def compose(self) -> ComposeResult:
        yield Static(self.content, id="help")

    def on_mount(self) -> None:
        box = self.query_one("#help")
        box.border_title = "? Aide"
        box.border_subtitle = "Échap pour fermer"
        if self.color:
            box.styles.border = ("round", self.color)
        if self.fg:
            box.styles.color = self.fg
        if self.bg:
            box.styles.background = self.bg

    def action_close(self) -> None:
        self.dismiss()


class Board(Container):
    """Zone où les panneaux sont placés à la main (positions absolues)."""

    def on_resize(self, event: events.Resize) -> None:
        self.app.apply_layout()


# ───────────── application ─────────────
GLOBAL_OK = {"quit", "help", "edit", "save_layout", "escape"}


class Dash(App):
    TITLE = "dash"
    CSS = """
    Board { width: 100%; height: 1fr; overflow: hidden; }
    Panel, Media, Procs, Term { position: absolute; display: none; padding: 0 1; border: round $primary; }
    Procs { border: round $warning; }
    .-selected { border: heavy $accent; }
    #filter { dock: bottom; margin-bottom: 1; display: none; }
    """
    BINDINGS = [
        Binding("space", "media('play-pause')", "Pause/Lecture"),
        Binding("n", "media('next')", "Suivant", show=False),
        Binding("p", "media('previous')", "Précédent", show=False),
        Binding("plus,greater_than_sign", "volume('5%+')", "Vol+", show=False),
        Binding("less_than_sign", "volume('5%-')", "Vol-", show=False),
        Binding("m", "mute('toggle')", "Muet", show=False),
        Binding("u", "mute('0')", "Démuter", show=False),
        Binding("d", "notif_rm", "Notif", show=False),
        Binding("D", "notif_clear", "Vider notifs", show=False),
        Binding("slash,f", "filter", "Filtre"),
        Binding("escape", "escape", "Échap", show=False),
        Binding("up", "proc_move(-1)", show=False),
        Binding("down", "proc_move(1)", show=False),
        Binding("c", "sort('cpu_percent')", "Tri CPU", show=False),
        Binding("r", "sort('memory_percent')", "Tri RAM", show=False),
        Binding("k", "kill('TERM')", "Kill", show=False),
        Binding("K", "kill('KILL')", "Kill -9", show=False),
        Binding("X", "kill_all", "Kill filtre", show=False),
        Binding("w", "weathr_restart", "weathr", show=False),
        Binding("e", "edit", "Édition"),
        Binding("S", "save_layout", "Enregistrer"),
        # --- mode édition (actifs seulement dans ce mode, voir check_action) ---
        Binding("tab", "edit_select(1)", "Panneau", priority=True),
        Binding("shift+tab", "edit_select(-1)", "Panneau préc.", show=False, priority=True),
        Binding("left", "edit_move('left')", "Colonne", key_display="←/→"),
        Binding("right", "edit_move('right')", "Colonne", show=False),
        Binding("up", "edit_move('up')", "Ordre", key_display="↑/↓"),
        Binding("down", "edit_move('down')", "Ordre", show=False),
        Binding("shift+left,a", "edit_size('w',-1)", "Taille", key_display="⇧+flèches"),
        Binding("shift+right,d", "edit_size('w',1)", "Large", show=False),
        Binding("shift+up,w", "edit_size('h',-1)", "Court", show=False),
        Binding("shift+down,s", "edit_size('h',1)", "Haut", show=False),
        Binding("t", "edit_unit", "Fixe/Flex"),
        Binding("R", "edit_reset", "Défaut"),
        Binding("question_mark", "help", "Aide"),
        Binding("q", "quit", "Quitter"),
    ]

    def __init__(self, pve: bool = False, theme: str | None = None, btop: dict | None = None,
                 weathr_cmd: str | None = None, warn: str | None = None, weathr_colors: str = "terminal"):
        super().__init__(ansi_color=theme is None)
        self.weathr_colors = weathr_colors
        self.pve = pve
        self.theme_name = theme
        self.btop = btop or {}
        self.weathr_cmd = weathr_cmd
        self.warn = warn
        self.order = [p for p in ORDER if p != "pve" or pve]
        ids = set(self.order)
        raw_defaults = make_defaults(weathr_cmd is not None, pve)
        self.defaults = {m: prune_layout(raw_defaults[m], self.order) for m in raw_defaults}
        self.saved = {m: v for m, v in load_saved().items() if m in self.defaults and prune_layout(v, self.order)}
        self.layouts = {
            m: copy.deepcopy(prune_layout(self.saved[m], self.order) if m in self.saved else self.defaults[m])
            for m in self.defaults
        }
        self.cur_mode = "wide"
        self.rects: dict = {}
        self.edit_mode = False
        self.sel: str | None = None
        self.box_colors: dict = {}
        user = CONFIG.get("colors")
        user = user if isinstance(user, dict) else {}
        self.color_names = {**DEFAULT_COLORS, **{k: str(v) for k, v in user.items() if k in DEFAULT_COLORS}}
        self.pending = None
        self.pending_at = 0.0

    def compose(self) -> ComposeResult:
        with Board(id="main"):
            yield Panel(T("sys"), sysinfo, 1, id="sys")
            if self.weathr_cmd:
                yield Term(T("weather"), self.weathr_cmd, self.weathr_colors, id="weather")
            else:
                yield Panel(T("weather"), weather, 600, id="weather")
            yield Panel(T("notifs"), notifs, 5, id="notifs")
            yield Panel(T("perf"), perf, 1, id="perf")
            yield Media(T("player"), id="player")
            yield Procs(T("procs"), id="procs")
            if self.pve:
                yield Panel(T("pve"), pve_status, 5, id="pve")
        yield Input(placeholder="filtre processus — Entrée valide, Échap efface", id="filter", disabled=True)
        yield Footer()

    # ---- thème ----
    def on_mount(self) -> None:
        if self.theme_name:
            try:
                self.theme = self.theme_name
            except Exception:
                self.notify(f"thème inconnu : {self.theme_name}", severity="error")
        else:
            for name in ("ansi-dark", "textual-ansi"):  # couleurs du terminal (nom selon la version)
                try:
                    self.theme = name
                    break
                except Exception:
                    continue
        self.apply_colors()
        if self.btop:
            self.apply_btop(self.btop)
        if self.warn:
            self.notify(self.warn, severity="warning", timeout=8)
        self.apply_layout()

    def resolve_color(self, name: str) -> str:
        role = ROLE_OF.get(name)
        if self.theme_name and role:  # thème nommé : couleurs du thème
            try:
                v = self.theme_variables.get(role)
                if v:
                    return v
            except Exception:
                pass
        return to_color(name)

    def apply_colors(self) -> None:
        for pid in self.order:
            col = self.resolve_color(self.color_names[pid])
            self.box_colors[pid] = col
            w = self.query_one("#" + pid)
            w.styles.border = ("round", col)
            w.styles.border_title_color = col
            w.styles.border_subtitle_color = col

    def apply_btop(self, c: dict) -> None:
        if c.get("main_bg"):
            self.screen.styles.background = c["main_bg"]
        boxes = {
            "sys": "cpu_box", "perf": "cpu_box", "player": "mem_box",
            "weather": "net_box", "notifs": "net_box", "procs": "proc_box", "pve": "mem_box",
        }
        for pid, key in boxes.items():
            try:
                w = self.query_one("#" + pid)
            except Exception:
                continue
            if c.get(key):
                self.box_colors[pid] = c[key]
                w.styles.border = ("round", c[key])
            if c.get("title"):
                w.styles.border_title_color = c["title"]
            if c.get("main_fg"):
                w.styles.color = c["main_fg"]

    # ---- disposition ----
    def mode_name(self) -> str:
        w = self.size.width
        return "narrow" if w < 90 else "medium" if w < 150 else "wide"

    def visible_ids(self) -> list:
        """Panneaux affichés, dans l'ordre colonnes gauche→droite, haut→bas."""
        lay = self.layouts[self.cur_mode]
        return [pid for c in lay["cols"] for pid, _ in c["panels"]
                if pid not in lay["hidden"] and self.query_one("#" + pid).display]

    def apply_layout(self) -> None:
        board = self.query_one("#main")
        W, H = board.size.width, board.size.height
        if W <= 0 or H <= 0:
            return
        self.cur_mode = self.mode_name()
        lay = self.layouts[self.cur_mode]
        shown = {p for p in self.order if p not in lay["hidden"]}
        self.rects = compute_rects(lay, shown, W, H)
        for pid in self.order:
            w = self.query_one("#" + pid)
            r = self.rects.get(pid)
            if r and r[1] < H and r[2] >= MIN_W and min(r[3], H - r[1]) >= MIN_H:
                x, y, ww, hh = r
                w.display = True
                w.styles.offset = (x, y)
                w.styles.width = ww
                w.styles.height = min(hh, H - y)
            else:
                w.display = False  # masqué ou pas la place
                if isinstance(w, Term):
                    w.stop()  # inutile de faire tourner weathr s'il est caché
        if self.edit_mode and self.sel not in self.visible_ids():
            vis = self.visible_ids()
            self.sel = vis[0] if vis else None
        self.refresh_selection()

    def size_label(self, pid: str) -> str:
        lay = self.layouts[self.cur_mode]
        pos = find_panel(lay, pid)
        if not pos:
            return ""
        ci, pi = pos
        spec = lay["cols"][ci]["panels"][pi][1]
        return f"col ×{lay['cols'][ci]['w']} · haut {spec if is_fr(spec) else spec + ' l.'}"

    def refresh_selection(self) -> None:
        for pid in self.order:
            w = self.query_one("#" + pid)
            sel = self.edit_mode and pid == self.sel
            w.set_class(sel, "-selected")
            w.border_subtitle = self.size_label(pid) if sel else ("" if not isinstance(w, Term) else w.border_subtitle)
            col = (self.btop.get("hi_fg") or self.resolve_color("bright_white")) if sel else self.box_colors.get(pid)
            if col:
                w.styles.border = ("heavy" if sel else "round", col)

    def on_resize(self, event: events.Resize) -> None:
        self.apply_layout()

    def check_action(self, action: str, parameters) -> bool | None:
        if isinstance(self.screen, HelpScreen):
            return action == "quit"
        if action.startswith("edit_"):
            return self.edit_mode
        if self.edit_mode:
            return action in GLOBAL_OK
        return True

    # ---- touches numériques : afficher / masquer ----
    def on_key(self, event: events.Key) -> None:
        if isinstance(self.screen, HelpScreen):
            return
        n = DIGITS.get(event.character or "")
        if n is None:
            return
        event.stop()
        lay = self.layouts[self.cur_mode]
        if n == 0:
            lay["hidden"] = list(self.defaults[self.cur_mode]["hidden"])
        elif n <= len(ORDER):
            pid = ORDER[n - 1]
            if pid not in self.order:
                self.notify("lance tdash avec -p pour le panneau Proxmox", severity="warning")
                return
            if pid in lay["hidden"]:
                lay["hidden"].remove(pid)
            else:
                lay["hidden"].append(pid)
        self.apply_layout()

    # ---- mode édition ----
    def action_edit(self) -> None:
        self.set_edit(not self.edit_mode)

    def set_edit(self, on: bool) -> None:
        self.edit_mode = on
        if on:
            self.screen.set_focus(None)
            vis = self.visible_ids()
            if self.sel not in vis:
                self.sel = vis[0] if vis else None
            self.notify(
                "Édition : ←→ colonne · ↑↓ ordre · ⇧+flèches (ou a d w s) taille · t fixe/flex · "
                "Tab suivant · S enregistrer · R défaut · Échap quitter",
                timeout=10,
            )
        self.refresh_bindings()
        self.refresh_selection()

    def action_escape(self) -> None:
        if self.edit_mode:
            self.set_edit(False)
        else:
            self.action_clear_filter()

    def action_edit_select(self, d: int) -> None:
        vis = self.visible_ids()
        if not vis:
            return
        i = vis.index(self.sel) if self.sel in vis else -1
        self.sel = vis[(i + d) % len(vis)]
        self.refresh_selection()

    def action_edit_move(self, direction: str) -> None:
        if not self.sel:
            return
        lay = self.layouts[self.cur_mode]
        if direction in ("up", "down"):
            move_v(lay, self.sel, -1 if direction == "up" else 1, set(lay["hidden"]))
        else:
            move_h(lay, self.sel, -1 if direction == "left" else 1)
        self.apply_layout()

    def action_edit_size(self, axis: str, d: int) -> None:
        if not self.sel:
            return
        lay = self.layouts[self.cur_mode]
        (resize_w if axis == "w" else resize_h)(lay, self.sel, d)
        self.apply_layout()

    def action_edit_unit(self) -> None:
        if self.sel:
            r = self.rects.get(self.sel)
            toggle_unit(self.layouts[self.cur_mode], self.sel, r[3] if r else 6)
            self.apply_layout()

    def action_edit_reset(self) -> None:
        self.layouts[self.cur_mode] = copy.deepcopy(self.defaults[self.cur_mode])
        if self.saved.pop(self.cur_mode, None) is not None:
            write_saved(self.saved)
        self.notify(f"disposition « {self.cur_mode} » remise par défaut")
        self.apply_layout()

    def action_save_layout(self) -> None:
        self.saved[self.cur_mode] = copy.deepcopy(self.layouts[self.cur_mode])
        try:
            write_saved(self.saved)
            self.notify(f"disposition « {self.cur_mode} » enregistrée ({LAYOUT_FILE})")
        except OSError as e:
            self.notify(f"échec de l'enregistrement : {e}", severity="error")

    # ---- aide ----
    def action_help(self) -> None:
        c = self.btop
        self.push_screen(HelpScreen(build_help(self.weathr_cmd is not None), c.get("cpu_box") or self.resolve_color(self.color_names["help"]), c.get("main_fg"), c.get("main_bg")))

    def action_weathr_restart(self) -> None:
        try:
            self.query_one("#weather").restart()
        except Exception:
            self.notify("pas de panneau weathr (lance avec -w)", severity="warning")

    # ---- filtre processus ----
    def action_filter(self) -> None:
        lay = self.layouts[self.cur_mode]
        if "procs" in lay["hidden"]:
            lay["hidden"].remove("procs")
            self.apply_layout()
        inp = self.query_one("#filter", Input)
        inp.disabled = False
        inp.display = True
        inp.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.query_one(Procs).set_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.display = False
        event.input.disabled = True
        self.screen.set_focus(None)

    def action_clear_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.value = ""
        inp.display = False
        inp.disabled = True
        self.screen.set_focus(None)
        self.query_one(Procs).set_filter("")

    def action_proc_move(self, delta: int) -> None:
        t = self.query_one(Procs)
        if t.display:
            t.action_cursor_down() if delta > 0 else t.action_cursor_up()

    def action_sort(self, key: str) -> None:
        t = self.query_one(Procs)
        t.sort_key = key
        t.refresh_data()

    # ---- kill ----
    def confirm(self, key, msg: str) -> bool:
        now = time.time()
        if self.pending == key and now - self.pending_at < 5:
            self.pending = None
            return True
        self.pending, self.pending_at = key, now
        self.notify(f"{msg} — appuie encore pour confirmer", severity="warning")
        return False

    def do_kill(self, pids, sig) -> None:
        ok = 0
        for pid in pids:
            if pid in (1, os.getpid()):
                continue
            try:
                psutil.Process(pid).send_signal(sig)
                ok += 1
            except Exception as e:
                self.notify(f"{pid} : {e}", severity="error")
        self.notify(f"{sig.name} envoyé à {ok} processus")
        self.poke("#procs")

    def action_kill(self, sig: str) -> None:
        t = self.query_one(Procs)
        if not t.display or not t.row_count or t.cursor_row >= len(t.procs):
            self.notify("affiche le panneau processus (touche 7)", severity="warning")
            return
        pid, name = t.procs[t.cursor_row]
        s = signal.SIGKILL if sig == "KILL" else signal.SIGTERM
        if self.confirm((sig, pid), f"{s.name} sur {name} ({pid})"):
            self.do_kill([pid], s)

    def action_kill_all(self) -> None:
        t = self.query_one(Procs)
        if not t.filter_text:
            self.notify("tape d'abord un filtre avec /", severity="warning")
            return
        pids = list(t.matches)
        if not pids:
            self.notify("aucun processus ne correspond", severity="warning")
            return
        if self.confirm(("all", t.filter_text, len(pids)), f"SIGTERM sur {len(pids)} processus « {t.filter_text} »"):
            self.do_kill(pids, signal.SIGTERM)

    # ---- média / volume / notifs ----
    def poke(self, selector: str) -> None:
        try:
            widget = self.query_one(selector)
        except Exception:
            return
        if hasattr(widget, "refresh_data"):
            self.set_timer(0.3, widget.refresh_data)

    def action_media(self, cmd: str) -> None:
        run(f"playerctl {cmd}")
        self.poke("#player")

    def show_volume(self) -> None:
        v = get_volume()
        if v:
            self.notify(f"Volume {v[0]:.0f}%{' (muet)' if v[1] else ''}", timeout=1.5)

    def action_volume(self, delta: str) -> None:
        run(f"wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ {delta}")
        self.set_timer(0.25, self.show_volume)
        self.poke("#sys")

    def action_mute(self, mode: str) -> None:
        run(f"wpctl set-mute @DEFAULT_AUDIO_SINK@ {mode}")
        self.set_timer(0.25, self.show_volume)
        self.poke("#sys")

    def action_notif_rm(self) -> None:
        if NOTIF_IDS:
            run(f"dunstctl history-rm {NOTIF_IDS[0]}")
            self.poke("#notifs")

    def action_notif_clear(self) -> None:
        run("dunstctl history-clear")
        self.poke("#notifs")


def init_config() -> None:
    """Copie la config d'exemple vers ~/.config/dash/config.toml (sans écraser)."""
    from importlib.resources import files

    dest = Path.home() / ".config/dash/config.toml"
    if dest.exists():
        print(f"{dest} existe déjà : non modifié")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((files("tdash") / "config.example.toml").read_text())
    dest.chmod(0o600)  # contient le secret du token Proxmox
    print(f"créé : {dest}\nédite-le (host, user, token…), puis lance : tdash --pve-check")


def main() -> None:
    global CITY
    ap = argparse.ArgumentParser(description="dashboard TUI")
    ap.add_argument("-p", "--pve", action="store_true", help="afficher le statut Proxmox (API)")
    ap.add_argument("--city", help="ville pour la météo")
    ap.add_argument("-w", "--weathr", action="store_true", help="météo animée avec weathr dans le panneau Météo")
    ap.add_argument("--theme", help="thème Textual (nord, gruvbox, dracula, catppuccin-mocha, tokyo-night…) au lieu des couleurs du terminal")
    ap.add_argument("--btop-theme", help="nom ou fichier d'un thème btop à réutiliser")
    ap.add_argument("--init", action="store_true", help="crée ~/.config/dash/config.toml à partir de l'exemple")
    ap.add_argument("--version", action="store_true", help="affiche la version")
    ap.add_argument("--pve-check", action="store_true", help="diagnostic de la connexion Proxmox (token, droits)")
    ap.add_argument("--reset-layout", action="store_true", help="supprime la disposition enregistrée")
    args = ap.parse_args()
    if args.version:
        from importlib.metadata import version

        print("tdash", version("tdash"))
        return
    if args.init:
        init_config()
        return
    CONFIG.update(load_config())
    if args.pve_check:
        pve_check()
        return
    CITY = args.city or CONFIG.get("city", CITY)
    theme = args.theme or CONFIG.get("theme") or None
    btop_name = args.btop_theme or CONFIG.get("btop_theme") or None
    btop = load_btop_theme(btop_name) if btop_name else {}
    if btop_name and not btop:
        print(f"thème btop introuvable : {btop_name}")
    if args.reset_layout:
        LAYOUT_FILE.unlink(missing_ok=True)
    cmd, warn = None, None
    if args.weathr or CONFIG.get("weather_mode") == "weathr":
        cmd = CONFIG.get("weathr_cmd", "weathr")
        if not shutil.which(shlex.split(cmd)[0]):
            cmd, warn = None, "weathr introuvable (cargo install weathr, ou yay -S weathr) — météo texte à la place"
    Dash(pve=args.pve, theme=theme, btop=btop, weathr_cmd=cmd, warn=warn,
         weathr_colors=CONFIG.get("weathr_colors", "terminal")).run()


if __name__ == "__main__":
    main()

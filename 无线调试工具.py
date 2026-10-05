#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
无线调试快速连接工具
====================
电脑自动生成二维码 + 配对码，手机扫码即可完成 Android 无线调试配对并自动连接；
启动时后台静默扫描已配对设备，配对 / 连接 / 断开 / 装 APK 一条龙。
• 启动时自动连接已配对过的设备（后台进行，不阻塞菜单）
• 菜单 [3]/[5]/[9] 只有一台设备时自动处理，无需手选
• 菜单 [6] 删除设备后自动断开其连接（当作陌生设备）
• 菜单 [7] 全屏拖放装 APK：把 apk 文件拖进窗口即安装，双击 Esc 退出
• adb shell 连按两次 Esc（或两次 Ctrl+C）退出
• 同一台手机在 adb devices 里只留一条记录
  （禁用 adb 的 mDNS 自动连接 + 自动清掉重复 / 失效的旧记录）
• 退出时自动清理 adb 后台进程
全程直接调用 adb 命令（每个命令执行前都会打印出来，可自行复制到终端运行）。

用法：python 无线调试工具.py   或双击 启动工具.bat
依赖：adb（可用内置 platform-tools）+ Python 包 qrcode / Pillow
"""

import json
import os
import random
import secrets
import shutil
import string
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

# ---------- Windows 控制台中文 / 二维码正常显示 ----------
if os.name == "nt":
    os.system("")                       # 启用 ANSI 转义
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass
if not sys.stdout.isatty():             # 管道/重定向时避免编码报错
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
DEVICES_FILE = BASE_DIR / "devices.json"
QR_PNG = BASE_DIR / "配对二维码.png"

CONFIG = {"adb_path": None}             # 设置：自定义 adb 路径
DEVICES = {}                            # 设备库 {标识: {name, model, endpoints}}

ADB_TIMEOUT = 15                        # 单条 adb 命令超时（秒）
PAIR_WAIT = 120                         # 扫码配对最长等待（秒）
DOUBLE_PRESS_WINDOW = 1.5               # 连按两次同一按键的判定窗口（秒）
DROP_IDLE = 0.45                        # 拖放/粘贴后停顿多久算「输入完成」（秒）
MDNS_CONNECT_TYPE = "_adb-tls-connect._tcp"   # 无线调试可连接服务的 mDNS 类型
MDNS_TRANSPORT_MARK = "._adb-tls-connect._tcp"  # adb 自动连接生成的传输名后缀
DEFAULT_APK_OPTIONS = "-r -d -t"        # 默认装包参数：替换安装 + 允许降级 + 允许测试包
APK_INSTALL_TIMEOUT = 300               # [7] 单次 adb install 最长等待（秒），0 = 不限时
PLATFORM_TOOLS_URL = ("https://dl.google.com/android/repository/"
                      "platform-tools-latest-windows.zip")

_print_lock = threading.Lock()          # 保护多线程输出
_state_lock = threading.Lock()          # 保护配置/设备库写盘
_BG_STOP = threading.Event()            # 退出时通知后台扫描线程停止
_cleanup_done = False                   # adb 清理只执行一次（多路径触发时幂等）
_console_handler_ref = None             # 保持控制台事件处理器的引用
_prompt_active = threading.Event()      # 主线程正停在「请选择 >」等待输入
_fullscreen = threading.Event()         # 正处于全屏拖放模式（后台消息先排队）
_fs_queue = []                          # 全屏模式下来不及显示的后台消息
PROMPT_TEXT = "  请选择 > "             # 菜单提示符（后台插播消息后需原样画回）


def say(msg="", end="\n"):
    with _print_lock:
        print(msg, end=end, flush=True)


def say_async(msg):
    """
    后台线程输出：既不弄丢消息，也不吃掉「请选择 >」提示符。
    若主线程正在菜单等输入：清掉当前行 → 打印消息 → 重画提示符。
    若主线程在全屏拖放模式：先排队，等界面重画时显示，避免把画面打乱。
    """
    with _print_lock:
        if _fullscreen.is_set():
            _fs_queue.append(msg)
            return
        at_prompt = _prompt_active.is_set()
        if at_prompt:
            print("\r" + " " * max(len(PROMPT_TEXT), 20) + "\r", end="", flush=True)
        print(msg, flush=True)
        if at_prompt:
            print(PROMPT_TEXT, end="", flush=True)     # 提示符原样画回，消息与提示符都在


def _ask_menu():
    """打印菜单提示符并读入一行；等待期间后台线程可安全插播消息。"""
    with _print_lock:                       # 提示符与标志位在同一把锁内，避免与插播抢序
        print(PROMPT_TEXT, end="", flush=True)
        _prompt_active.set()
    try:
        return input().strip()
    finally:
        _prompt_active.clear()


# ============================================================
# 配置读写
# ============================================================
def load_state():
    global CONFIG, DEVICES
    try:
        CONFIG = json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception:
        CONFIG = {"adb_path": None}
    try:
        DEVICES = json.loads(DEVICES_FILE.read_text("utf-8"))
    except Exception:
        DEVICES = {}
    changed = False
    for k, v in (("autoconnect_on_start", True),
                 ("kill_adb_on_exit", True),
                 ("disable_mdns_autoconnect", True),       # 修重复设备：禁用 adb 的 mDNS 自动连接
                 ("apk_install_options", DEFAULT_APK_OPTIONS),
                 ("apk_install_timeout", APK_INSTALL_TIMEOUT)):   # 装包超时（秒），0=不限时
        if k not in CONFIG:
            CONFIG[k] = v
            changed = True
    if changed:
        save_state()


def save_state():
    with _state_lock:
        CONFIG_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), "utf-8")
        DEVICES_FILE.write_text(json.dumps(DEVICES, ensure_ascii=False, indent=2), "utf-8")


# ============================================================
# adb 封装（唯一出口，执行前打印命令）
# ============================================================
def find_adb():
    """查找 adb.exe：已保存路径 → 内置 platform-tools → PATH → 常见安装位置"""
    if CONFIG.get("adb_path") and Path(CONFIG["adb_path"]).is_file():
        return CONFIG["adb_path"]
    local = BASE_DIR / "platform-tools" / "adb.exe"
    if local.is_file():
        return str(local)
    from shutil import which
    w = which("adb")
    if w:
        return w
    for pat in (r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe",
                r"%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe",
                r"C:\Android\platform-tools\adb.exe",
                r"C:\platform-tools\adb.exe",
                r"C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe"):
        p = Path(os.path.expandvars(pat))
        if p.is_file():
            return str(p)
    return None


def adb_path():
    adb = find_adb()
    if not adb:
        say("× 未找到 adb。请先在菜单 [8] ADB设置 中指定 adb.exe 位置。")
    return adb


def _adb_env():
    """
    所有 adb 子进程统一使用的环境变量。
    关键一条：ADB_MDNS_AUTO_CONNECT=0 → 关掉 adb 自己的 mDNS 自动连接。
    （adb 默认会自动连 _adb-tls-connect._tcp 服务，会额外生成一条
      adb-XXX._adb-tls-connect._tcp 传输，和本工具 adb connect IP:端口 那条
      指向同一台手机，导致 adb devices 里出现「两个设备」。）
    """
    env = os.environ.copy()
    if CONFIG.get("disable_mdns_autoconnect", True):
        env["ADB_MDNS_AUTO_CONNECT"] = "0"
    return env


def adb(*args, show=True, timeout=ADB_TIMEOUT):
    """执行 adb 命令，返回 (输出文本, 退出码)。"""
    exe = adb_path()
    if exe is None:
        return "", 1
    cmd = [exe, *args]
    if show:
        say(f"  $ {subprocess.list2cmdline(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_adb_env())
    except subprocess.TimeoutExpired:
        return "[命令超时]", 1
    except FileNotFoundError:
        return f"[无法执行：{exe}]", 1
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    text = out + (("\n" + err) if err.strip() else "")
    return text.strip(), r.returncode


def adb_ok(*args, **kw):
    text, code = adb(*args, **kw)
    return code == 0, text


def restart_adb_server():
    adb("kill-server", show=False)
    adb("start-server", show=False)


def init_adb_env():
    """
    让 adb 服务在「禁用 mDNS 自动连接」的环境下运行（修重复设备问题的根）。
    ADB_MDNS_AUTO_CONNECT 是服务端进程读取的，所以必须让服务本身带着它启动：
    • 服务由本次启动 → 环境已生效，什么都不用做
    • 已经有别的工具（Android Studio / 旧版本工具）起的服务在跑 → 重启一次让它生效
    可在 config.json 里用 "disable_mdns_autoconnect": false 关掉这套处理。
    """
    if not CONFIG.get("disable_mdns_autoconnect", True):
        return
    os.environ["ADB_MDNS_AUTO_CONNECT"] = "0"
    if find_adb() is None:
        return
    out, _ = adb("start-server", show=False)
    if "daemon not running" in out:
        return                          # 服务刚由我们拉起，环境变量已经生效
    restart_adb_server()                # 服务是别人起的 → 重启一次应用设置
    say("  · 已让 adb 服务在「禁用 mDNS 自动连接」的环境下重启"
        "（避免同一台手机在 adb devices 里出现两条记录）")


# ============================================================
# mDNS 发现（用 adb 自带能力，零额外依赖）
# ============================================================
def mdns_services():
    """解析 `adb mdns services` → [ {name,type,addr}, ... ]"""
    text, _ = adb("mdns", "services", show=False)
    svc = []
    for line in text.splitlines():
        parts = line.split()
        # 形如：  adb-XXXX-QXjCrW  _adb-tls-pairing._tcp  192.168.1.5:33861
        if len(parts) == 3 and parts[2].count(":") == 1:
            svc.append({"name": parts[0], "type": parts[1], "addr": parts[2]})
    return svc


# ============================================================
# 设备库
# ============================================================
def remember_device(key, model="", endpoints=None):
    with _state_lock:                       # 后台线程与主线程可能同时写入
        d = DEVICES.setdefault(key, {"name": "", "model": "", "endpoints": []})
        if model:
            d["model"] = model
        for ep in (endpoints or []):
            if ep in d["endpoints"]:
                d["endpoints"].remove(ep)
            d["endpoints"].insert(0, ep)
        d["endpoints"] = d["endpoints"][:5]
        if not d["name"]:
            d["name"] = model or key
    save_state()


def device_model(target):
    text, _ = adb("-s", target, "shell", "getprop", "ro.product.model",
                  show=False, timeout=8)
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[0].strip() if lines else ""


def device_label(target):
    """给设备加个人话名字：设备库里的名称 + 当前地址。"""
    ip = target.rsplit(":", 1)[0] if ":" in target else ""
    if ip:
        for key, rec in DEVICES.items():
            for ep in rec.get("endpoints", []):
                if ep.rsplit(":", 1)[0] == ip:
                    return f"{rec.get('name') or key}  ({target})"
    return target


def get_online_devices():
    """[(序列号/地址, 状态), ...]"""
    text, _ = adb("devices", show=False)
    out = []
    for line in text.splitlines()[1:]:
        p = line.split()
        if len(p) >= 2 and p[1] in ("device", "unauthorized", "offline"):
            out.append((p[0], p[1]))
    return out


def online_serials():
    """当前状态为 device 的设备列表。"""
    return [sn for sn, st in get_online_devices() if st == "device"]


def is_device_alive(serial):
    """设备此刻还连着吗（安装过程中用；掉线时 adb install 会一直挂着不返回）。"""
    try:
        return any(sn == serial or sn.startswith(serial) for sn in online_serials())
    except Exception:
        return True                     # 查不到就别乱中止，交给超时兜底


# ============================================================
# 重复 / 失效记录清理
# ============================================================
def is_mdns_transport(serial):
    """是否 adb 自动连接（mDNS）生成的传输，形如 adb-XXXX._adb-tls-connect._tcp"""
    return MDNS_TRANSPORT_MARK in serial


def device_ip_of(target):
    """拿设备当前 IP（取 ip route 输出里 src 后面的地址）。"""
    text, _ = adb("-s", target, "shell", "ip", "route", show=False, timeout=8)
    toks = text.split()
    for i, t in enumerate(toks):
        if t == "src" and i + 1 < len(toks) and toks[i + 1].count(".") == 3:
            return toks[i + 1]
    return ""


def mdns_ip_of_transport(serial):
    """mDNS 传输名 → IP：用 `adb mdns services` 里的实例名匹配。"""
    instance = serial.split(MDNS_TRANSPORT_MARK)[0]
    for s in mdns_services():
        if s["name"] == instance and MDNS_CONNECT_TYPE in s["type"]:
            return s["addr"].rsplit(":", 1)[0]
    return ""


def dedupe_devices(verbose=False):
    """
    让 adb devices 里每台手机只留一条在线记录（修「两个 adb 设备」）。
    处理两类多余记录：
      1) adb 自动连接生成的 mDNS 传输（adb-XXX._adb-tls-connect._tcp）
         —— 与 adb connect IP:端口 那条指向同一台手机，保留 IP:端口 那条
      2) 同一台手机遗留的 offline / unauthorized 旧端口记录
    返回清理掉的条数。
    """
    devs = get_online_devices()
    if not devs:
        return 0
    live = [(sn, st) for sn, st in devs if st == "device"]
    mdns_addrs = {s["addr"].rsplit(":", 1)[0]: s["addr"]
                  for s in mdns_services() if MDNS_CONNECT_TYPE in s["type"]}
    fixed = 0

    # --- 1) mDNS 自动连接的传输 ---
    for sn, _st in [d for d in devs if is_mdns_transport(d[0])]:
        ip = mdns_ip_of_transport(sn) or device_ip_of(sn)
        peer = [p for p, _ in live
                if not is_mdns_transport(p) and ip and p.rsplit(":", 1)[0] == ip]
        if not peer:
            # 这台手机当前只有 mDNS 这一条 → 先补一条 IP:端口 再断开，别把设备弄丢
            addr = mdns_addrs.get(ip)
            if not (addr and try_connect_addr(addr, quiet=True)):
                continue
        text, _ = adb("disconnect", sn, show=False, timeout=8)
        if "disconnected" in text.lower():
            fixed += 1
            if verbose:
                say(f"  √ 已清掉重复记录：{sn}")

    # --- 2) 同一台手机遗留的失效（offline / unauthorized）记录 ---
    live_ips = {sn.rsplit(":", 1)[0] for sn, _ in live
                if not is_mdns_transport(sn) and ":" in sn}
    for sn, st in devs:
        if st in ("offline", "unauthorized") and not is_mdns_transport(sn) and ":" in sn:
            if sn.rsplit(":", 1)[0] in live_ips:
                text, _ = adb("disconnect", sn, show=False, timeout=8)
                if "disconnected" in text.lower():
                    fixed += 1
                    if verbose:
                        say(f"  √ 已清掉失效记录：{sn} （{st}）")
    return fixed


# ============================================================
# 自动连接（启动时 / 配对后 / 设备唯一时）
# ============================================================
def mdns_connect_addrs():
    """mDNS 里当前可连接的地址列表（端口每次都会变，必须实时取）。"""
    return [s["addr"] for s in mdns_services() if MDNS_CONNECT_TYPE in s["type"]]


def is_online(target):
    """target 可为序列号、IP:端口或设备库键「名@IP」；在线可调试返回 True。"""
    online = online_serials()
    for sn in online:
        if sn == target or sn.startswith(target):
            return True
    ips = set()
    if "@" in target:
        ips.add(target.rsplit("@", 1)[1])          # 设备库键：名@IP
    if target.count(".") == 3 and ":" in target:
        ips.add(target.rsplit(":", 1)[0])         # 直接给的 IP:端口
    for sn in online:
        if sn.rsplit(":", 1)[0] in ips:
            return True
    return False


def try_connect_addr(addr, quiet=True):
    """尝试连接一个地址，成功返回 True。"""
    text, _ = adb("connect", addr, show=not quiet, timeout=6)
    if "connected to" in text.lower() or "already connected" in text.lower():
        if not quiet:
            say(f"  √ 已连接：{addr}")
        return True
    return False


def autoconnect_record(rec_key, rec=None, quiet=False):
    """
    自动连接一台已配对设备：
    1) 优先用 mDNS 实时发现的服务（按 IP 匹配）——无线调试端口每次都会变
    2) mDNS 没有时，再逐个试历史地址（IP:端口）
    返回 (是否成功, 连上的地址, 设备名)
    """
    if rec is None:
        rec = DEVICES.get(rec_key, {})
    name = rec.get("name") or rec_key
    history = rec.get("endpoints", []) or []
    hosts = {ep.rsplit(":", 1)[0] for ep in history}

    def _remember(addr):
        # 连接成功后写回设备库；但若用户刚在菜单[6]删了它，则不再复活
        if rec_key in DEVICES:
            remember_device(rec_key, device_model(addr), [addr])

    for addr in mdns_connect_addrs():           # 1) mDNS 实时地址
        if addr.rsplit(":", 1)[0] in hosts and try_connect_addr(addr):
            _remember(addr)
            if not quiet:
                say(f"  √ 已自动连接：{name}  {addr}")
            return True, addr, name
    for ep in history:                          # 2) 历史地址逐个试
        if try_connect_addr(ep):
            _remember(ep)
            if not quiet:
                say(f"  √ 已自动连接：{name}  {ep}")
            return True, ep, name
    return False, "", name


def bg_autoconnect_worker():
    """
    后台静默扫描线程：遍历设备库自动连接。
    • 扫描不到：不做任何提示（完全静默）
    • 扫到并连上：在主线程打印「√ 已自动连接：设备名 IP:端口」
    """
    try:
        # mDNS 服务列表刚启动时可能为空，先安静地等一会儿
        for _ in range(3):
            if _BG_STOP.is_set():
                return
            if mdns_connect_addrs():
                break
            _BG_STOP.wait(1)
        for key, rec in list(DEVICES.items()):
            if _BG_STOP.is_set():
                return
            if is_online(key):                  # 已在线：静默跳过
                continue
            ok, addr, name = autoconnect_record(key, rec, quiet=True)
            if ok:
                say_async(f"  √ 已自动连接：{name}  {addr}")
    except Exception:
        pass                                    # 后台线程绝不向前台抛错


def start_bg_autoconnect():
    """启动时开一个后台守护线程做自动连接扫描（不阻塞菜单显示）。"""
    if not DEVICES or not CONFIG.get("autoconnect_on_start", True):
        return None
    t = threading.Thread(target=bg_autoconnect_worker, daemon=True,
                         name="bg-autoconnect")
    t.start()
    return t


def cleanup_adb(verbose=True):
    """退出时清理 adb 后台进程（可在 config.json 里用 kill_adb_on_exit=false 关闭）。
    幂等：窗口关闭事件与正常退出的清理路径可能同时触发，只真正执行一次。
    """
    global _cleanup_done
    if _cleanup_done:
        return
    if not CONFIG.get("kill_adb_on_exit", True):
        return
    _cleanup_done = True
    exe = find_adb()
    if not exe:
        return
    try:
        # 超时压在 Windows 关闭事件宽限窗口（约 5 秒）之内，避免点 X 时被强杀
        subprocess.run([exe, "kill-server"], capture_output=True, timeout=4,
                       env=_adb_env())
        if verbose:
            say("  √ 已清理 adb 后台进程（adb kill-server）")
    except Exception:
        pass


def _console_event(event):
    """控制台事件回调（Windows）：0=Ctrl+C  1=Ctrl+Break  2=关闭窗口(X)  5=注销  6=关机。
    点右上角 X 关闭窗口时不会走 Python 的 finally，清理工作在这里做。
    """
    try:
        _BG_STOP.set()
        cleanup_adb(verbose=False)
    except Exception:
        pass
    return False                    # 返回 False，让系统按默认流程终止进程


def install_console_handler():
    """
    捕获控制台关闭事件，保证「点窗口右上角 X 关闭」时也能清理 adb。
    原理：Windows 在用户点 X / 注销 / 关机时会向控制台控制处理器发送
    CTRL_CLOSE_EVENT（此时不会走 Python 的 finally），在这里做清理。
    """
    global _console_handler_ref
    if os.name != "nt" or _console_handler_ref is not None:
        return
    try:
        import ctypes
        from ctypes import wintypes
        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        _console_handler_ref = HANDLER(_console_event)   # 必须全局持引用，否则会被 GC
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True)
    except Exception:
        _console_handler_ref = None


# ============================================================
# [1] 扫码配对：电脑出码，手机扫码
# ============================================================
def show_qr(service, code):
    payload = f"WIFI:T:ADB;S:{service};P:{code};;"
    say(f"  二维码内容：{payload}")
    try:
        import qrcode
    except ImportError:
        say("  × 缺少 qrcode 库，正在自动安装...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "qrcode", "pillow",
                            "--disable-pip-version-check", "--quiet"])
        if r.returncode != 0:
            say("  × 自动安装失败，可手动执行：pip install qrcode pillow")
            say("  （仍可用菜单 [2] 手动配对，不影响使用）")
            return
    qr = qrcode.QRCode(border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    qrcode.make(payload, box_size=10, border=4).save(str(QR_PNG))
    say(f"  已保存图片：{QR_PNG.name}（终端扫不动时，可打开此图片扫）")


def find_connect_endpoint(host, wait_s=10):
    """等待并返回与 host 同一 IP 的 _adb-tls-connect 地址（端口每次都会变，必须实时取）。"""
    for _ in range(wait_s):
        time.sleep(1)
        for s in mdns_services():
            if (MDNS_CONNECT_TYPE in s["type"]
                    and s["addr"].rsplit(":", 1)[0] == host):
                return s["addr"]
    return ""


def pair_finish(host):
    """
    配对成功后的统一收尾：自动连接（不再询问）。
    返回是否已连上。
    """
    endpoint = find_connect_endpoint(host)
    key_hit, name = "", ""
    for key, rec in DEVICES.items():        # 设备库里同 IP 的既有记录（取名称）
        if any(ep.rsplit(":", 1)[0] == host for ep in rec.get("endpoints", [])):
            key_hit, name = key, rec.get("name") or key
            break
    if not endpoint and key_hit:            # mDNS 没发现时，退回该设备的历史地址
        eps = DEVICES.get(key_hit, {}).get("endpoints", [])
        endpoint = eps[0] if eps else ""
    if not endpoint:
        say("\n  ! 配对成功，但暂未发现可连接的服务（手机无线调试开关可能被关闭）。")
        say("    稍后可在菜单 [3] 连接。")
        return False
    if try_connect_addr(endpoint, quiet=False):
        model = device_model(endpoint)
        remember_device(key_hit or f"{model or '设备'}@{host}", model, [endpoint])
        say(f"  √ 自动连接成功：{name or model or host}  {endpoint}")
        say("  已存入设备库。")
        return True
    say(f"  × 自动连接失败，请确认手机无线调试已开启（地址 {endpoint}），稍后可用菜单 [3] 重试。")
    return False


def pair_and_connect(addr, code):
    """对地址执行 adb pair 配对；成功后显示候选连接并询问是否连接。"""
    say(f"\n  发现手机配对服务：{addr}")
    ok, text = adb_ok("pair", addr, code)
    if not ok or "Successfully paired" not in text:
        say(f"  × 配对失败：{text or '无输出'}")
        return False
    say("  √ 配对成功！")
    pair_finish(addr.rsplit(":", 1)[0])
    return True


def menu_pair_qr():
    say("\n===== 扫码配对（推荐） =====")
    say("手机操作：设置 → 开发者选项 → 无线调试（保持开启）→ 使用二维码配对设备 → 扫码")
    say("前提：手机与电脑连同一 WiFi")
    # 生成服务名与配对码（官方协议格式）
    rand = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    service = f"studio-{rand}"
    code = str(random.randint(100000, 999999))
    say(f"\n  服务名：{service}")
    say(f"  配对码：{code}（手机扫码后自动使用，无需输入）")
    show_qr(service, code)
    say(f"\n正在等待手机扫码（最长 {PAIR_WAIT} 秒，期间请不要关闭本窗口）...")
    start, tried = time.time(), set()
    while time.time() - start < PAIR_WAIT:
        for s in mdns_services():
            if "_adb-tls-pairing._tcp" not in s["type"]:
                continue
            # 只认我们二维码里的服务名（别的设备扫码不会叫这个名字）
            if s["name"].startswith(service) and s["addr"] not in tried:
                tried.add(s["addr"])
                if pair_and_connect(s["addr"], code):
                    return True
        time.sleep(2)
    say("  × 等待超时。请检查：")
    say("    1) 手机是否完成扫码、无线调试开关是否开启")
    say("    2) 手机与电脑是否在同一 WiFi")
    say("    3) 电脑防火墙是否放行 adb（mDNS 发现需要 UDP 5353）")
    return False


# ============================================================
# [2] 手动配对
# ============================================================
def menu_pair_manual():
    say("\n===== 手动配对 =====")
    say("手机操作：设置 → 开发者选项 → 无线调试 → 使用配对码配对设备")
    say("手机弹窗会显示「IP地址和端口」+ 6 位配对码，填到下方：")
    addr = input("  IP:端口（如 192.168.1.5:37025）> ").strip().replace("：", ":")
    if not addr:
        return
    code = input("  6 位配对码 > ").strip()
    if not code:
        return
    ok, text = adb_ok("pair", addr, code)
    if not ok or "Successfully paired" not in text:
        say(f"  × 配对失败：{text or '无输出'}")
        say("  常见原因：配对码/端口已过期、手机弹窗已关闭、不在同一 WiFi")
        return
    say("  √ 配对成功！")
    pair_finish(addr.rsplit(":", 1)[0])


# ============================================================
# [3] 连接设备
# ============================================================
def connect_one(addr):
    ok, text = adb_ok("connect", addr)
    if "connected to" not in text.lower() and "already connected" not in text.lower():
        say(f"  × 连接失败：{text}")
        return False
    say(f"  √ 已连接：{addr}")
    model = device_model(addr)
    remember_device(f"{model or '设备'}@{addr.rsplit(':', 1)[0]}", model, [addr])
    dedupe_devices()                        # 连上后顺手清掉重复 / 失效记录
    return True


def menu_connect():
    say("\n===== 连接设备 =====")
    dedupe_devices()
    found = [s for s in mdns_services() if MDNS_CONNECT_TYPE in s["type"]]
    names = list(DEVICES.keys())
    # 设备库记录若与 mDNS 发现的地址同一 IP，视为同一台设备，不再重复列出
    found_ips = {s["addr"].rsplit(":", 1)[0] for s in found}
    lib_only = [k for k in names
                if not any(ep.rsplit(":", 1)[0] in found_ips
                           for ep in DEVICES[k].get("endpoints", []))]
    total = len(found) + len(lib_only)
    if total == 0:
        say("  没有可连接的设备。请先用菜单 [1] 扫码配对 或 [2] 手动配对。")
        return
    if total == 1:                          # 只有一台设备 → 按 IP:端口直接自动连接
        if found:
            addr = found[0]["addr"]
            say(f"  仅检测到一台设备：{addr}，自动连接...")
            if try_connect_addr(addr, quiet=False):
                model = device_model(addr)
                remember_device(f"{model or '设备'}@{addr.rsplit(':', 1)[0]}", model, [addr])
                say(f"  √ 已连接并入库：{model or addr}")
                dedupe_devices()
            else:
                say("  × 连接失败，请确认手机无线调试开关已打开。")
            return
        key = lib_only[0]
        rec = DEVICES[key]
        say(f"  设备库中仅一台设备：{rec.get('name') or key}，自动连接...")
        ok, addr, name = autoconnect_record(key, rec)
        if not ok:
            say(f"  × 自动连接失败：{name}（mDNS 与历史地址均未连上）")
            say("    IP 可能已变化，请重开手机无线调试后用菜单 [1] 重新扫码。")
        return
    if found:                               # 多台设备：正常列表选择
        say("  自动发现：")
        for i, s in enumerate(found, 1):
            say(f"    [{i}] {s['addr']}（在线）")
    if lib_only:
        say("  设备库：")
        for i, k in enumerate(lib_only, 1 + len(found)):
            d = DEVICES[k]
            eps = " / ".join(d.get("endpoints", [])) or "无历史地址"
            say(f"    [{i}] {d.get('name') or k}   {eps}")
    say("    [m] 手动输入 IP:端口")
    say("    [0] 返回")
    c = input("  选择 > ").strip().lower()
    if c in ("", "0"):
        return
    if c == "m":
        addr = input("  IP:端口 > ").strip().replace("：", ":")
        if addr:
            connect_one(addr)
        return
    try:
        idx = int(c)
    except ValueError:
        return
    if 1 <= idx <= len(found):
        connect_one(found[idx - 1]["addr"])
    elif 1 <= idx - len(found) <= len(lib_only):
        key = lib_only[idx - len(found) - 1]
        d = DEVICES[key]
        say(f"  正在连接 {d.get('name') or key} ...")
        ok, addr, name = autoconnect_record(key, d)      # 同样优先 mDNS 实时端口
        if ok:
            dedupe_devices()
        else:
            say(f"  × {name} 连不上（mDNS 与历史地址均失败）。")
            say("    IP 可能已变，请用自动发现或手动输入，也可重开手机无线调试后重新扫码。")


# ============================================================
# [4] 设备列表  [5] 断开
# ============================================================
def menu_list():
    say("\n===== 设备列表 =====")
    if dedupe_devices(verbose=True):
        say("  （已自动清理重复/失效记录）")
    text, _ = adb("devices", "-l")
    say()
    for ln in text.splitlines():
        if ln.strip():
            say("  " + ln)
    if not get_online_devices():
        say("  （当前没有已连接的设备）")


def menu_disconnect():
    say("\n===== 断开设备 =====")
    dedupe_devices()
    devs = get_online_devices()
    if not devs:
        say("  当前没有已连接的设备。")
        return
    if len(devs) == 1:                      # 只有一台设备：直接断开，不用选
        sn, st = devs[0]
        say(f"  仅有一台设备：{sn}（{st}），直接断开...")
        adb("disconnect", sn)
        return
    for i, (sn, st) in enumerate(devs, 1):
        say(f"    [{i}] {sn}  ({st})")
    say("    [a] 断开全部   [0] 返回")
    c = input("  选择 > ").strip().lower()
    if c == "a":
        adb("disconnect")
    elif c in ("", "0"):
        return
    else:
        try:
            idx = int(c)
            if 1 <= idx <= len(devs):
                adb("disconnect", devs[idx - 1][0])
        except (ValueError, IndexError):
            pass


# ============================================================
# [6] 设备库管理
# ============================================================
def menu_manage():
    say("\n===== 设备库管理 =====")
    if not DEVICES:
        say("  设备库为空。完成一次配对/连接后会自动保存设备。")
        return
    names = list(DEVICES.keys())
    for i, k in enumerate(names, 1):
        d = DEVICES[k]
        say(f"    [{i}] {d.get('name') or k}   型号:{d.get('model') or '?'}   "
            f"地址:{' / '.join(d.get('endpoints', [])) or '无'}")
    say("    [0] 返回")
    try:
        idx = int(input("  选择要管理的设备 > ").strip())
        if not (1 <= idx <= len(names)):
            return
    except ValueError:
        return
    k = names[idx - 1]
    say(f"  已选：{DEVICES[k].get('name')}（{k}）")
    c = input("  [1]重命名  [2]删除  [0]返回 > ").strip()
    if c == "1":
        new = input("  新名称 > ").strip()
        if new:
            DEVICES[k]["name"] = new
            save_state()
            say("  √ 已保存")
    elif c == "2":
        if input("  确认删除？(y/n) > ").strip().lower() == "y":
            # 删除设备库记录的同时断开该设备连接（当作陌生设备处理）
            eps = list(DEVICES[k].get("endpoints", []))
            hosts = {ep.rsplit(":", 1)[0] for ep in eps}
            targets = {sn for sn, st in get_online_devices()
                       if sn.rsplit(":", 1)[0] in hosts}
            targets |= set(eps)                 # 历史地址也一并断开
            DEVICES.pop(k, None)
            save_state()
            for t in targets:
                text, _ = adb("disconnect", t, timeout=8)
                if "disconnected" in text.lower() or "no such device" in text.lower():
                    say(f"  √ 已断开：{t}")
            if not targets:
                say("  （该设备当前未连接，无需断开）")
            say("  √ 已删除（已当作陌生设备处理）")


# ============================================================
# 终端按键：连按两次 Esc / Ctrl+C 退出（shell 与拖放模式共用）
# ============================================================
class DoubleKeyExit:
    """状态机：判定是否触发「连按两次 Esc」或「连按两次 Ctrl+C」退出。
    返回 "exit"=应退出，"hint"=第一次按下（可提示用户），None=普通输入。
    """
    def __init__(self, window=DOUBLE_PRESS_WINDOW):
        self.window = window
        self.last_esc = 0.0
        self.last_ctrl_c = 0.0

    def feed(self, key, now):
        if key == "esc":
            if now - self.last_esc <= self.window:
                return "exit"
            self.last_esc, self.last_ctrl_c = now, 0.0
            return "hint"
        if key == "ctrl_c":
            if now - self.last_ctrl_c <= self.window:
                return "exit"
            self.last_ctrl_c, self.last_esc = now, 0.0
            return "hint"
        self.last_esc = self.last_ctrl_c = 0.0      # 普通输入 → 重置计数
        return None


def _read_key(msvcrt):
    """读取一个按键，归一化为 esc / ctrl_c / 字符 / ""（功能键）/ None（无键）。"""
    if not msvcrt.kbhit():
        return None
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):            # Windows 功能键前缀，丢弃后半个码
        if msvcrt.kbhit():
            msvcrt.getwch()
        return ""
    if ch == "\x1b":
        # 区分三种情况：单按 Esc / 连按两次 Esc / VT 方向键序列（\x1b[ 或 \x1bO）
        time.sleep(0.04)
        if msvcrt.kbhit():
            nxt = msvcrt.getwch()
            if nxt in ("[", "O"):          # CSI / SS3 序列 → 吞掉整段
                for _ in range(8):
                    if msvcrt.kbhit():
                        if "@" <= msvcrt.getwch() <= "~":
                            break
                    else:
                        time.sleep(0.01)
                return ""
            try:
                msvcrt.ungetwch(nxt)        # 不是序列 → 退回，按普通键处理
            except Exception:
                pass
        return "esc"
    if ch == "\x03":
        return "ctrl_c"
    return ch


def _set_processed_input(enable):
    """
    开关控制台的 ENABLE_PROCESSED_INPUT。
    关闭后 Ctrl+C 不再直接变成中断信号，而是像普通按键一样从 getwch() 读到，
    这样「连按两次 Ctrl+C 退出」才能真正生效（返回原模式，供恢复用）。
    """
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)                     # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return None
        old = mode.value
        new = (old | 0x0001) if enable else (old & ~0x0001)
        k.SetConsoleMode(h, new)
        return old
    except Exception:
        return None


def _restore_processed_input(old):
    """恢复控制台原来的输入模式。"""
    if old is None:
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-10), old)
    except Exception:
        pass


# ============================================================
# [7] 安装 APK（全屏拖放）
# ============================================================
ANSI_RST = "\x1b[0m"
ANSI_DIM = "\x1b[90m"
ANSI_CYAN = "\x1b[36m"
ANSI_GREEN = "\x1b[32m"
ANSI_RED = "\x1b[31m"
ANSI_BOLD = "\x1b[1m"


def _dwidth(text):
    """字符串在终端里的显示宽度（中日韩全角字符算 2 列）。"""
    w = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _fit(text, width):
    """按显示宽度截断到 width 列（超出部分用 … 收尾）。"""
    if width <= 0:
        return ""
    if _dwidth(text) <= width:
        return text
    out, w = "", 0
    for ch in text:
        cw = _dwidth(ch)
        if w + cw > max(0, width - 1):
            break
        out += ch
        w += cw
    return out + "…"


def _pad(text, width, align="left"):
    """按显示宽度补齐到 width 列。"""
    text = _fit(text, width)
    space = max(0, width - _dwidth(text))
    if align == "center":
        left = space // 2
        return " " * left + text + " " * (space - left)
    if align == "right":
        return " " * space + text
    return text + " " * space


def _split_paths(text):
    """把「拖入/粘贴的一行文本」拆成路径列表：支持英文引号、空格分隔、多个文件。"""
    text = (text or "").strip()
    if not text:
        return []
    out, cur, in_quote = [], "", False
    for ch in text:
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch == " " and not in_quote:
            if cur:
                out.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return [p.strip() for p in out if p.strip()]


def _apk_paths(text):
    return [p for p in _split_paths(text) if p.lower().endswith(".apk")]


def _human_size(path):
    try:
        n = float(Path(path).stat().st_size)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


_INSTALL_FAIL_HINTS = {
    "INSTALL_FAILED_VERSION_DOWNGRADE": "设备上已有更高版本，可在 config.json 的 apk_install_options 里加 -d",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "签名不一致，需先卸载设备上的旧版本",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "手机存储空间不足",
    "INSTALL_FAILED_INVALID_APK": "APK 文件损坏或不完整",
    "INSTALL_FAILED_TEST_ONLY": "测试包，需要在安装参数里加 -t",
    "INSTALL_FAILED_USER_RESTRICTED": "被 MIUI/系统安装限制拦截，请手动允许安装",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "APK 未签名",
}


def _install_reason(text):
    """从 adb install 的输出里提炼一行结果说明。"""
    text = (text or "").strip()
    if "Success" in text:
        return "安装成功"
    if text.startswith("["):                # 工具自己生成的中止原因（超时/离线/取消）
        head, _, tail = text[1:].partition("]")
        tail = tail.strip()
        if head.startswith("安装超时"):
            return "安装超时中止" + (f"（{tail}）" if tail else "")
        return head + (f"（{tail}）" if tail else "")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("Failure") or "INSTALL_" in line or "Error" in line:
            for code, hint in _INSTALL_FAIL_HINTS.items():
                if code in line:
                    return f"{code}（{hint}）"
            return line
    return text.splitlines()[-1].strip() if text else "无输出"


def _run_install(serial, apk, opts, tick=None, timeout=0, should_cancel=None):
    """
    执行 adb -s <serial> install <opts> <apk>；返回 (是否成功, 输出或原因, 用时秒)。

    三重保险，避免「一直卡在正在安装」：
      1) 总超时：超过 timeout 秒仍未结束就掐掉（timeout=0 表示不限时）
      2) 设备掉线：安装期间设备离线的话 adb 会一直挂着，连续两次探测不到就中止
      3) 手动取消：should_cancel() 返回 True 时立刻中止（拖放界面里按 Esc）
    """
    exe = adb_path()
    if not exe:
        return False, "未找到 adb", 0.0
    cmd = [exe, "-s", serial, "install", *opts, apk]
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=_adb_env())
    except Exception as e:
        return False, f"无法执行：{e}", 0.0
    chunks = []

    def _reader():
        try:
            for raw in p.stdout:
                chunks.append(raw)
        except Exception:
            pass

    def _kill():
        for fn in (p.kill, p.terminate):
            try:
                fn()
                return
            except Exception:
                continue

    th = threading.Thread(target=_reader, daemon=True)
    th.start()
    aborted, offline_hits, last_check = "", 0, 0.0
    while p.poll() is None:
        el = time.time() - t0
        if tick:
            try:
                tick(el)
            except Exception:
                pass
        if timeout and el >= timeout:
            aborted = f"安装超时：超过 {int(timeout)} 秒仍未完成，已中止"
            _kill()
            break
        if should_cancel and should_cancel():
            aborted = "已手动取消安装（Esc）"
            _kill()
            break
        if el - last_check >= 3.0:                  # 每 3 秒探一次设备还在不在
            last_check = el
            if is_device_alive(serial):
                offline_hits = 0
            else:
                offline_hits += 1
                if offline_hits >= 2:
                    aborted = "设备已离线，安装无法继续，已中止"
                    _kill()
                    break
        time.sleep(0.2)
    th.join(timeout=2)
    cost = time.time() - t0
    text = b"".join(chunks).decode("utf-8", "replace").strip()
    if aborted:
        tail = text.splitlines()[-1].strip() if text.strip() else ""
        return False, f"[{aborted}]" + (f" 最后输出：{tail}" if tail else ""), cost
    return ("Success" in text and p.returncode == 0), text, cost


def _draw_apk_screen(ctx):
    """
    把整个终端刷成拖放安装界面（全屏）。
    最后一行永远是「路径 >」输入行，光标停在那里，方便边看边拖。
    """
    try:
        cols, rows = shutil.get_terminal_size((80, 24))
    except Exception:
        cols, rows = 80, 24
    W = max(44, min(cols - 2, 76))
    R = max(14, rows)
    dz_h = 5 if R >= 18 else 3
    n_res = max(0, min(6, R - 13 - dz_h))

    if _fs_queue:                                   # 后台消息先看一眼再清掉
        ctx["status"] = _fs_queue[-1].strip()
        _fs_queue.clear()

    lines = ["\x1b[2J\x1b[H"]
    left = "  APK 拖放安装  ·  全屏模式"
    right = "双击 Esc 退出  "
    lines.append(ANSI_CYAN + ANSI_BOLD + _pad(left, W - _dwidth(right)) + right + ANSI_RST)
    lines.append(ANSI_DIM + "  " + "─" * (W - 4) + ANSI_RST)
    lines.append(f"  目标设备：{_fit(ctx['label'], W - 12)}")
    limit = ctx.get("timeout") or 0
    param = (f"  安装参数：adb install {' '.join(ctx['opts'])}"
             + (f"   ·   超时 {int(limit)}s" if limit else "   ·   超时 不限"))
    lines.append(ANSI_DIM + _fit(param, W) + ANSI_RST)

    box_w = W - 4
    inner = box_w - 2
    if ctx["busy"]:
        content = ["", f"正在安装：{ctx['busy']}", ""]
    else:
        content = ["",
                   "把 APK 文件直接拖进本窗口 → 立刻安装到上面这台设备",
                   "也可以粘贴 / 输入路径，回车安装",
                   ""]
    inner_rows = dz_h - 2
    while len(content) < inner_rows:
        content.append("")
    content = content[:inner_rows]
    lines.append("")
    lines.append("  ╔" + "═" * inner + "╗")
    for ln in content:
        lines.append("  ║" + ANSI_CYAN + _pad(ln, inner, "center") + ANSI_RST + "║")
    lines.append("  ╚" + "═" * inner + "╝")

    lines.append(ANSI_DIM + "  最近安装：" + ANSI_RST)
    shown = ctx["results"][-n_res:] if n_res else []
    if not shown:
        lines.append(ANSI_DIM + "    （还没有安装记录）" + ANSI_RST)
    for ok, name, reason, size, cost in shown:
        plain = f"  [{'√' if ok else '×'}] {name}   {size}   {reason}   用时 {cost:.1f}s"
        lines.append((ANSI_GREEN if ok else ANSI_RED) + _fit(plain, W) + ANSI_RST)

    lines.append(ANSI_DIM + _fit("  $ " + ctx["cmd"], W) + ANSI_RST if ctx["cmd"] else "")
    lines.append(_fit("  " + ctx["status"], W) if ctx["status"] else "")
    if ctx["busy"]:
        lines.append(ANSI_DIM + "  安装中：按 Esc 可取消本次安装（双击 Esc 退出全屏）" + ANSI_RST)
    else:
        lines.append(ANSI_DIM + "  拖入即装 · 回车=安装输入的路径 · [d]=换设备 · 双击 Esc 退出" + ANSI_RST)
    lines.append("")
    sys.stdout.write("\n".join(lines) + "\n" + ANSI_BOLD + "  路径 > " + ANSI_RST + ctx["buf"])
    sys.stdout.flush()


def _fs_install(ctx, text):
    """处理一次「拖入 / 回车」：解析路径 → 逐个安装 → 记录结果。"""
    ctx["status"] = ""
    ctx["cmd"] = ""
    paths = _apk_paths(text)
    if not paths:
        raw = _split_paths(text)
        if raw:
            ctx["status"] = "× 只支持 .apk 文件：" + raw[0]
        else:
            ctx["status"] = "× 没识别到文件路径，请把 apk 拖进窗口"
        return
    if not is_online(ctx["serial"]):
        ctx["status"] = "× 设备已断开，请回主菜单重新连接后再试"
        return
    for p in paths:
        path = Path(p)
        if not path.is_file():
            ctx["results"].append((False, path.name, "文件不存在", "?", 0.0))
            continue
        size = _human_size(path)
        ctx["busy"] = f"{path.name}  ({size})"
        _draw_apk_screen(ctx)
        name = path.name
        limit = ctx.get("timeout") or 0

        def tick(elapsed, _n=name, _l=limit):
            tail = f" / 最长 {int(_l)}s（Esc 取消）" if _l else "（Esc 取消）"
            sys.stdout.write(f"\r  {ANSI_CYAN}正在安装 {_n} … {elapsed:.1f}s{tail}{ANSI_RST}\x1b[K")
            sys.stdout.flush()

        ok, out, cost = _run_install(ctx["serial"], str(path), ctx["opts"], tick,
                                     timeout=limit,
                                     should_cancel=ctx.get("cancel_check"))
        ctx["busy"] = ""
        ctx["cmd"] = subprocess.list2cmdline(
            [find_adb() or "adb", "-s", ctx["serial"], "install", *ctx["opts"], str(path)])
        reason = _install_reason(out)
        ctx["results"].append((ok, name, reason, size, cost))
        if ok:
            ctx["status"] = f"√ {name} 安装完成（{cost:.1f}s）"
        elif "超时" in reason:
            ctx["status"] = (f"× {name} 安装超时中止（{cost:.1f}s）"
                             f"· 大包/网速慢可按 [8]→[7] 调大装包超时")
        elif "离线" in reason:
            ctx["status"] = f"× {name} 安装中止：设备离线。请回主菜单重新连接后再装"
        elif "取消" in reason:
            ctx["status"] = f"× {name} 已取消安装"
        else:
            ctx["status"] = f"× {name} 安装失败：{reason}"
    _draw_apk_screen(ctx)


def _esc_pressed(msvcrt):
    """
    安装过程中「看一眼」是否按了 Esc（用来取消安装）。
    与 _read_key 的区别：不是 Esc 的键会原样退回，不会被吞掉，所以不影响拖放/输入。
    """
    try:
        if not msvcrt.kbhit():
            return False
        ch = msvcrt.getwch()
    except Exception:
        return False
    if ch == "\x1b":
        time.sleep(0.03)
        if msvcrt.kbhit():
            nxt = msvcrt.getwch()
            if nxt in ("[", "O"):           # 方向键等 VT 序列 → 吞掉整段，不算 Esc
                for _ in range(8):
                    if msvcrt.kbhit():
                        if "@" <= msvcrt.getwch() <= "~":
                            break
                    else:
                        time.sleep(0.01)
                return False
            try:
                msvcrt.ungetwch(nxt)        # 不是序列 → 退回，按普通键处理
            except Exception:
                pass
        return True
    try:
        msvcrt.ungetwch(ch)                 # 不是 Esc → 退回去，不丢用户输入
    except Exception:
        pass
    return False


def _apk_drop_loop(serial, label):
    """全屏拖放安装循环。返回 "exit"（正常退出）或 "switch"（要求换设备）。"""
    try:
        import msvcrt
    except ImportError:
        say("  × 当前系统不支持拖放模式，请直接用 adb install。")
        return "exit"

    opts = [o for o in
            (CONFIG.get("apk_install_options") or DEFAULT_APK_OPTIONS).split() if o]
    try:
        timeout = float(CONFIG.get("apk_install_timeout", APK_INSTALL_TIMEOUT) or 0)
    except (TypeError, ValueError):
        timeout = float(APK_INSTALL_TIMEOUT)
    ctx = {"serial": serial, "label": label, "opts": opts, "buf": "",
           "results": [], "status": "", "cmd": "", "busy": "",
           "timeout": max(0.0, timeout),
           "cancel_check": lambda: _esc_pressed(msvcrt)}
    ui = DoubleKeyExit()
    old_mode = _set_processed_input(False)          # 让 Ctrl+C 以按键形式到达
    _fullscreen.set()
    last_input = time.time()
    result = "exit"
    try:
        _draw_apk_screen(ctx)
        while True:
            key = _read_key(msvcrt)
            if key is None:
                # 拖入完成判定：输入框里是一串**真实存在**的 .apk 路径，且已经停顿
                hit = []
                if ctx["buf"] and time.time() - last_input >= DROP_IDLE:
                    hit = [p for p in _apk_paths(ctx["buf"]) if Path(p).is_file()]
                if hit:
                    text, ctx["buf"] = ctx["buf"], ""
                    _fs_install(ctx, text)
                    continue
                time.sleep(0.02)
                continue
            if key == "":                       # 功能键：忽略
                continue
            now = time.time()
            if key == "esc":
                if ui.feed("esc", now) == "exit":
                    break
                ctx["status"] = "再按一次 Esc 退出拖放模式"
                ctx["buf"] = ""
                _draw_apk_screen(ctx)
                continue
            if key == "ctrl_c":
                if ui.feed("ctrl_c", now) == "exit":
                    break
                ctx["status"] = "再按一次 Ctrl+C 退出拖放模式"
                ctx["buf"] = ""
                _draw_apk_screen(ctx)
                continue
            ui.feed("other", now)
            last_input = now
            if key in ("\r", "\n"):
                text, ctx["buf"] = ctx["buf"], ""
                if text.strip():
                    _fs_install(ctx, text)
                else:
                    _draw_apk_screen(ctx)
                continue
            if key == "\x08":                   # 退格
                if ctx["buf"]:
                    ctx["buf"] = ctx["buf"][:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if key == "d" and not ctx["buf"]:   # 输入框为空时按 d = 换设备
                result = "switch"
                break
            if key >= " ":                      # 可见字符：本地回显（拖入的路径也走这里）
                ctx["buf"] += key
                sys.stdout.write(key)
                sys.stdout.flush()
                continue
    except KeyboardInterrupt:
        pass
    finally:
        _fullscreen.clear()
        _fs_queue.clear()
        _restore_processed_input(old_mode)
        sys.stdout.write("\x1b[2J\x1b[H")       # 退出全屏，清干净
        sys.stdout.flush()
    return result


def menu_apk_install():
    say("\n===== 安装 APK（全屏拖放） =====")
    exe = adb_path()
    if not exe:
        return
    dedupe_devices()
    while True:
        devs = online_serials()
        if not devs:
            say("  当前没有在线设备。请先用 [1] 扫码配对 / [3] 连接设备。")
            return
        if len(devs) == 1:
            serial = devs[0]
            label = device_label(serial)
            say(f"  仅有一台在线设备：{label}")
        else:
            say("  当前有多台在线设备，选择安装到哪台：")
            for i, sn in enumerate(devs, 1):
                say(f"    [{i}] {device_label(sn)}")
            say("    [0] 返回")
            c = input("  选择 > ").strip()
            if c in ("", "0"):
                return
            try:
                idx = int(c)
            except ValueError:
                return
            if not (1 <= idx <= len(devs)):
                return
            serial = devs[idx - 1]
            label = device_label(serial)
        say("  进入全屏拖放模式（把 apk 文件拖进窗口即安装，双击 Esc 退出）...")
        time.sleep(0.35)
        if _apk_drop_loop(serial, label) == "switch":
            say("  （已退出拖放模式，重新选择设备）")
            continue
        say("  已退出拖放安装模式。")
        return


# ============================================================
# [8] ADB 设置
# ============================================================
def browse_file():
    """弹出文件选择框选 adb.exe（PowerShell，零依赖）。"""
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          "Add-Type -AssemblyName System.Windows.Forms;"
          "$f=New-Object System.Windows.Forms.OpenFileDialog;"
          "$f.Filter='adb.exe|adb.exe';$f.Title='选择 adb.exe';"
          "if($f.ShowDialog() -eq 'OK'){Write-Output $f.FileName}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                           capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace").strip() or None
    except Exception:
        return None


def download_platform_tools():
    """下载 Google 官方 platform-tools 并解压到本工具目录（内置 adb，开箱即用）。"""
    target = BASE_DIR / "platform-tools"
    if (target / "adb.exe").is_file():
        say("  √ 已存在内置 platform-tools，无需下载。")
        return True
    zip_path = BASE_DIR / "platform-tools-latest-windows.zip"
    say(f"  正在下载 Google 官方 platform-tools（约 8MB）...\n  {PLATFORM_TOOLS_URL}")
    ps = ("$ProgressPreference='SilentlyContinue';"
          f"Invoke-WebRequest -Uri '{PLATFORM_TOOLS_URL}' -OutFile '{zip_path}' "
          "-UseBasicParsing")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=600)
    except Exception as e:
        say(f"  × 下载失败：{e}")
        return False
    if r.returncode != 0 or not zip_path.is_file():
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        say(f"  × 下载失败：{err or '网络不可用，可手动下载后解压到本目录'}")
        return False
    say("  下载完成，正在解压...")
    ps2 = f"Expand-Archive -Path '{zip_path}' -DestinationPath '{BASE_DIR}' -Force"
    try:
        r2 = subprocess.run(["powershell", "-NoProfile", "-Command", ps2],
                            capture_output=True, timeout=300)
    except Exception as e:
        say(f"  × 解压失败：{e}")
        return False
    try:
        zip_path.unlink()
    except Exception:
        pass
    ok = (target / "adb.exe").is_file()
    say("  √ 已内置 adb：" + str(target / "adb.exe") if ok
        else "  × 解压后没找到 adb.exe，请手动解压 platform-tools 压缩包到本目录")
    return ok


def ensure_adb():
    """
    找不到 adb 时主动提示下载官方 platform-tools。
    给「仓库里不带 adb 二进制」的发行方式兜底：clone 下来直接双击就能用。
    """
    if find_adb() is not None:
        return True
    say("  ! 没有找到 adb。")
    say("    可以从 Google 官方下载 platform-tools（约 8MB）内置到本目录，下载后开箱即用。")
    try:
        c = input("  现在自动下载？(y/n) > ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    if c in ("y", "yes", "1"):
        if download_platform_tools():
            return True
    say("    也可以手动指定：菜单 [8] ADB设置 → [2] 输入路径 / [3] 文件浏览器选择")
    return False


def menu_adb_settings():
    say("\n===== ADB 设置 =====")
    cur = find_adb()
    say(f"  当前 adb：{cur or '未找到'}")
    if cur:
        ver, _ = adb("version", show=False)
        first = ver.splitlines()[0] if ver else ""
        if first:
            say(f"  版本：{first}")
    say(f"  mDNS 自动连接："
        f"{'已禁用（推荐，防止同一台手机出现两条记录）' if CONFIG.get('disable_mdns_autoconnect', True) else '已启用'}")
    tmo = CONFIG.get("apk_install_timeout", APK_INSTALL_TIMEOUT)
    say(f"  装包超时 [7]：{'不限时' if not tmo else str(int(tmo)) + ' 秒'}")
    say("  [1] 自动检测（清除自定义路径）  [2] 手动输入路径  [3] 文件浏览器选择")
    say("  [4] 重启 adb 服务              [5] 下载官方 platform-tools（内置 adb）")
    say("  [6] 开关「mDNS 自动连接」      [7] 设置装包超时      [0] 返回")
    c = input("  选择 > ").strip()
    if c == "1":
        CONFIG["adb_path"] = None
        save_state()
        say(f"  √ 已恢复自动检测：{find_adb() or '仍未找到，请手动指定'}")
    elif c == "2":
        p = input("  adb.exe 完整路径 > ").strip().strip('"')
        if p and Path(p).is_file():
            CONFIG["adb_path"] = p
            save_state()
            say(f"  √ 已保存：{p}")
        else:
            say("  × 文件不存在")
    elif c == "3":
        say("  正在打开文件选择框...")
        p = browse_file()
        if p and Path(p).is_file():
            CONFIG["adb_path"] = p
            save_state()
            say(f"  √ 已保存：{p}")
        else:
            say("  × 未选择有效文件")
    elif c == "4":
        adb("kill-server")
        adb("start-server")
        say("  √ adb 服务已重启")
    elif c == "5":
        download_platform_tools()
    elif c == "6":
        CONFIG["disable_mdns_autoconnect"] = not CONFIG.get("disable_mdns_autoconnect", True)
        save_state()
        if CONFIG["disable_mdns_autoconnect"]:
            restart_adb_server()
            say("  √ 已禁用 mDNS 自动连接（adb 服务已重启）")
        else:
            say("  ! 已启用 mDNS 自动连接：adb 会自己连一份，"
                "同一台手机可能在 adb devices 里出现两条记录")
    elif c == "7":
        say("  装 APK 的最长等待秒数（0 = 不限时）。大包 / 网速慢可以调大，比如 900。")
        v = input(f"  新超时秒数（当前 {int(tmo) if tmo else '不限'}）> ").strip()
        if not v:
            return
        try:
            n = max(0, int(float(v)))
        except ValueError:
            say("  × 请输入数字")
            return
        CONFIG["apk_install_timeout"] = n
        save_state()
        say(f"  √ 已保存：{'不限时' if not n else str(n) + ' 秒'}")


# ============================================================
# [9] adb shell
# ============================================================
def run_shell_with_quick_exit(exe, sn):
    """
    进入 adb shell：连按两次 Esc（或连按两次 Ctrl+C）退出；也支持输入 exit。
    键盘通过 msvcrt 逐键接管，检出双击后终止子进程回主菜单。
    """
    try:
        import msvcrt
    except ImportError:                     # 非 Windows：退化为普通 shell
        say("  （当前系统不支持双击退出，输入 exit 退出）")
        subprocess.run([exe, "-s", sn, "shell"])
        return
    say("  [提示] 连按两次 Esc（或连按两次 Ctrl+C）退出；也可输入 exit。")
    try:
        p = subprocess.Popen([exe, "-s", sn, "shell"], stdin=subprocess.PIPE,
                             env=_adb_env())
    except Exception as e:
        say(f"  × 无法启动 shell：{e}")
        return
    ui = DoubleKeyExit()
    buf = ""
    old_mode = _set_processed_input(False)          # 让 Ctrl+C 以按键形式到达
    try:
        while p.poll() is None:
            key = _read_key(msvcrt)
            if key is None:
                time.sleep(0.02)
                continue
            if key == "":                        # 功能键：忽略
                continue
            now = time.time()
            if key == "ctrl_c":
                r = ui.feed("ctrl_c", now)
                _send_line(p)                   # 也发一个中断信号给远端 shell
                if r == "exit":
                    break
                say("  [再按一次 Ctrl+C（或连按两次 Esc）即可退出]")
                continue
            if key == "esc":
                r = ui.feed("esc", now)
                if r == "exit":
                    break
                say("  [再按一次 Esc（或连按两次 Ctrl+C）即可退出]")
                continue
            ui.feed("other", now)
            if key in ("\r", "\n"):             # 回车
                _send_line(p, buf)
                buf = ""
            elif key == "\x08":                  # 退格（仅本地回显同步）
                buf = buf[:-1]
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            elif key == "\t":
                sys.stdout.write("\t")
                sys.stdout.flush()
            elif key >= " ":                     # 可见字符
                buf += key
                sys.stdout.write(key)
                sys.stdout.flush()
    except KeyboardInterrupt:                    # 控制台直接抛 Ctrl+C
        pass
    finally:
        _restore_processed_input(old_mode)       # 恢复终端的 Ctrl+C 行为
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=4)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    say("  已退出 shell。")


def _send_line(p, text=""):
    """把一行文本发给 adb shell（adb shell 一般需要 CRLF）。"""
    try:
        if p.stdin:
            p.stdin.write((text + "\r\n").encode("utf-8", "replace"))
            p.stdin.flush()
    except Exception:
        pass


def menu_shell():
    say("\n===== adb shell =====")
    dedupe_devices()
    devs = online_serials()
    if not devs:
        say("  当前没有在线设备。")
        return
    if len(devs) == 1:                      # 只有一台在线设备：直接进入，不用选
        sn = devs[0]
        say(f"  仅有一台在线设备：{sn}，直接进入 shell...")
    else:
        for i, sn in enumerate(devs, 1):
            say(f"    [{i}] {sn}")
        say("    [0] 返回")
        try:
            idx = int(input("  选择 > ").strip())
        except ValueError:
            return
        if not (1 <= idx <= len(devs)):
            return
        sn = devs[idx - 1]
    exe = adb_path()
    if exe:
        say(f"  进入 {sn} 的 shell ...")
        run_shell_with_quick_exit(exe, sn)


# ============================================================
# 主菜单
# ============================================================
MENU = """
================================================
        无线调试快速连接工具
================================================
  [1] 扫码配对(推荐)   电脑出二维码，手机扫码
  [2] 手动配对         输入手机显示的配对码
  [3] 连接设备         自动发现 / 设备库 / 手动
  [4] 设备列表         adb devices -l
  [5] 断开设备         adb disconnect
  [6] 设备库管理       重命名 / 删除
  [7] 安装APK          全屏拖放，拖进去就装
  [8] ADB设置          选adb路径 / 版本 / 重启
  [9] adb shell        进入交互式终端
  [0] 退出（自动清理 adb 后台进程）
------------------------------------------------"""


def main():
    load_state()
    install_console_handler()               # 点窗口 X 关闭时也能清理 adb
    ensure_adb()                            # 没找到 adb 时提示下载官方 platform-tools
    init_adb_env()                          # 禁用 adb 的 mDNS 自动连接（修重复设备）
    dedupe_devices()                        # 清掉上次可能残留的重复记录
    start_bg_autoconnect()                  # 后台静默扫描并自动连接（不阻塞菜单）
    try:
        while True:
            say(MENU)
            c = _ask_menu()
            acts = {"1": menu_pair_qr, "2": menu_pair_manual, "3": menu_connect,
                    "4": menu_list, "5": menu_disconnect, "6": menu_manage,
                    "7": menu_apk_install, "8": menu_adb_settings, "9": menu_shell}
            if c == "0":
                say("  再见！")
                return
            fn = acts.get(c)
            if fn:
                try:
                    fn()
                except KeyboardInterrupt:
                    say("\n  （已取消当前操作）")
    finally:
        _BG_STOP.set()                      # 通知后台线程停止
        cleanup_adb()                       # 退出/中断时清理 adb 后台进程


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        say("\n  已退出。")

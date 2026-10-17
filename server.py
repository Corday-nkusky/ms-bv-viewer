#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫雷 BV 成绩台 —— 本地服务

扫描扫雷（Arbiter 阿比特）目录下所有 .avf 录像，统计「每个级别 × 每个 BV 的
最快成绩」，用本地服务把结果喂给网页。只为消除浏览器直接读盘需要授权的麻烦。

- 纯标准库，零依赖（Python 3.8+）
- 只读：不修改任何录像文件
- 默认监听 127.0.0.1，自动挑空闲端口，启动后自动打开浏览器
- 平级放进扫雷安装目录（和 replays / replays2 同级）即可自动找到录像

用法：
    python server.py                        # 启动并打开页面
    python server.py --root "D:\\Arbiter"    # 指定扫雷目录
    python server.py --port 9000            # 指定端口
    python server.py --no-open              # 不自动打开浏览器
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _base_dirs():
    """返回 (程序所在目录, 静态资源目录)。

    源码运行时两者相同。PyInstaller 打成的单文件 exe 里，app.html 会被解压到
    临时目录（sys._MEIPASS），但「程序位置」必须取 exe 自身所在目录——否则
    会跑去 Temp 里找 replays，永远找不到。
    """
    if getattr(sys, "frozen", False):
        return (os.path.dirname(os.path.abspath(sys.executable)),
                getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable))))
    here = os.path.dirname(os.path.abspath(__file__))
    return here, here


HERE, RES_DIR = _base_dirs()
APP_HTML = os.path.join(RES_DIR, "app.html")
# 提示用户怎么带参数启动（exe 和源码两种形态）
LAUNCHER = (os.path.basename(sys.executable) if getattr(sys, "frozen", False)
            else "python server.py")

LEVEL_ORDER = ["beg", "int", "exp"]
LEVEL_CN = {"beg": "初级 Beg", "int": "中级 Int", "exp": "高级 Exp"}
LEVEL_CSV = {"BEG": "beg", "INT": "int", "EXP": "exp"}
SKIP_DIRS = {"__macosx", "node_modules", ".git", ".svn", ".hg", ".idea", ".vscode",
             "__pycache__", ".workbuddy", "$recycle.bin", "system volume information"}
MAX_DEPTH = 4          # 从扫描根往下最多找几层

NAME_RE = re.compile(
    r"^(?P<level>Beg|Int|Exp)_(?P<style>NF|FL)_(?P<time>[0-9]+\.[0-9]+)_"
    r"3BV=(?P<bv>[0-9]+)_3BVs=(?P<bvs>[0-9]+(?:\.[0-9]+)?)_(?P<name>.*)\.avf$",
    re.IGNORECASE,
)
AVF_TIME_RE = re.compile(rb"RealTime:\s*([0-9.]+)")
AVF_DATE_RE = re.compile(rb"([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})\.([0-9]{1,2}):([0-9]{2}):([0-9]{2})")

if hasattr(sys.stdout, "reconfigure"):
    try:
        # 只在输出被重定向（管道 / 文件）时强制 UTF-8；
        # 控制台里保持系统默认——Windows 控制台本身走 Unicode API，中文不会乱码。
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ------------------------------------------------------------------ 扫描

def _avf_count(d, cap=50000):
    """目录**自身**（不含子目录）里有几个 .avf。数到 cap 就停。"""
    n = 0
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.name.lower().endswith(".avf") and e.is_file():
                        n += 1
                        if n >= cap:
                            break
                except OSError:
                    continue
    except OSError:
        pass
    return n


def _scan_dirs(root):
    """列出所有需要扫描的录像目录（绝对路径）。

    阿比特（Arbiter）把录像放在 `replays` / `replays2` 这类目录里，所以优先取
    名字以 replay 开头且直接含 .avf 的目录；万一某个人的目录名不一样，再退回
    「任意直接含 .avf 的目录」，保证别人拿去也能用。
    """
    root = os.path.abspath(root)
    base = root.rstrip(os.sep).count(os.sep)
    replay, other = [], []
    root_self = False
    for dirpath, dirnames, _ in os.walk(root):
        if dirpath.rstrip(os.sep).count(os.sep) - base >= MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        if not _avf_count(dirpath):
            continue
        if dirpath == root:
            root_self = True           # 根目录自己也放着录像（直接用 --root 指向它）
        elif os.path.basename(dirpath).lower().startswith("replay"):
            replay.append(dirpath)
        else:
            other.append(dirpath)
    if not replay:
        # 没有 replay* 目录：退回「任意含 avf 的目录」；根目录自己也算一个
        return sorted(([root] if root_self else []) + other)
    if not replay:
        return sorted(other)
    # replay 目录存在时，只额外纳入它们下面的子目录（如 replays\新建文件夹），
    # 避免把无关位置的备份录像算进来。
    extra = [d for d in other if any(d.startswith(r + os.sep) for r in replay)]
    return sorted(replay + extra)


def iter_avf_dirs(root):
    for d in _scan_dirs(root):
        yield d


def _has_replay_child(d):
    """d 的**直接**子目录里有没有装着录像的 replay*（不递归，用来精确定位根目录）。"""
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_dir() and e.name.lower().startswith("replay") and _avf_count(e.path):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def _looks_like_root(d, max_children=300):
    """浅层判断 d 是不是扫雷目录：自己有 avf，或直接子目录里有 avf。
    只 scandir 两层，不递归，避免在用户目录里乱翻太久。"""
    if _avf_count(d):
        return True
    n = 0
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if not e.is_dir():
                        continue
                    n += 1
                    if n > max_children:
                        break
                    if _avf_count(e.path):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def discover_root(start=None):
    """猜「扫雷根目录」。

    本程序要和 replays/ replays2/ 放在同一个文件夹里，所以优先取程序所在
    目录的**上一层**；但为了别人随便放也能用，按下面顺序试：
      ① 程序自己所在目录就有录像          → 就是它
      ② 往上最多 3 层里，谁的直接子目录是 replay* → 就是它（正常安装属于这种）
      ③ 再看这些层的兄弟目录（仓库和扫雷目录并排）→ 就是它
      ④ 都没有                            → 退回上一层，由调用方提示用户
    """
    start = os.path.abspath(start or HERE)
    chain, cur = [], start
    while True:
        chain.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    if _avf_count(start) or _has_replay_child(start):
        return start
    for d in chain[1:4]:
        if _avf_count(d) or _has_replay_child(d):
            return d
    for d in chain[1:3]:
        try:
            subs = [e.path for e in os.scandir(d)
                    if e.is_dir() and e.name.lower() not in SKIP_DIRS]
        except OSError:
            subs = []
        for sub in subs:
            if _looks_like_root(sub):
                return sub
    return chain[1] if len(chain) > 1 else start


def signature(root):
    """目录指纹：文件数 + 总字节 + 最大 mtime。用于判断是否需要重新扫描。"""
    n = 0
    total = 0
    newest = 0.0
    for d in iter_avf_dirs(root):
        try:
            with os.scandir(d) as it:
                for e in it:
                    if not e.name.lower().endswith(".avf"):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    n += 1
                    total += st.st_size
                    if st.st_mtime > newest:
                        newest = st.st_mtime
        except OSError:
            continue
    return f"{n}:{total}:{int(newest)}"


def scan(root):
    games = {}
    unparsed = []
    dup = 0
    files_seen = 0
    dirs = []
    for d in iter_avf_dirs(root):
        dirs.append(os.path.relpath(d, root))
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".avf"):
                continue
            files_seen += 1
            m = NAME_RE.match(name)
            if not m:
                unparsed.append(os.path.join(os.path.relpath(d, root), name))
                continue
            if name in games:
                dup += 1
                continue
            games[name] = {
                "level": m.group("level").lower(),
                "style": m.group("style").upper(),
                "time": float(m.group("time")),
                "bv": int(m.group("bv")),
                "file": name,
                "path": os.path.join(os.path.relpath(d, root), name),
            }
    return list(games.values()), {
        "dirs": dirs,
        "files": files_seen,
        "unique": len(games),
        "duplicates": dup,
        "unparsed": len(unparsed),
        "unparsed_sample": unparsed[:5],
    }


def build_records(games):
    groups = {}
    for g in games:
        groups.setdefault((g["level"], g["bv"]), []).append(g)

    def better(a, b):
        if b is None:
            return a
        if a["time"] != b["time"]:
            return a if a["time"] < b["time"] else b
        return a if a["file"] < b["file"] else b

    rows = []
    for (level, bv), items in groups.items():
        best = nf = fl = None
        for it in items:
            best = better(it, best)
            if it["style"] == "NF":
                nf = better(it, nf)
            else:
                fl = better(it, fl)
        rows.append({
            "level": level,
            "bv": bv,
            "games": len(items),
            "time": round(best["time"], 2),
            "bvs": round(bv / best["time"], 2) if best["time"] else None,
            "style": best["style"],
            "file": best["file"],
            "path": best["path"],
            "nfTime": round(nf["time"], 2) if nf else None,
            "flTime": round(fl["time"], 2) if fl else None,
            "nfFile": nf["file"] if nf else None,
            "flFile": fl["file"] if fl else None,
            "nfGames": sum(1 for it in items if it["style"] == "NF"),
            "flGames": sum(1 for it in items if it["style"] == "FL"),
            "date": None,
            "realTime": None,
        })
    rows.sort(key=lambda r: (LEVEL_ORDER.index(r["level"]), r["bv"]))
    return rows


def verify_and_date(rows, root):
    """读每条记录录像的尾部，取内嵌 RealTime 与录制日期。"""
    ok = mismatch = missing = 0
    for r in rows:
        p = os.path.join(root, r["path"])
        try:
            with open(p, "rb") as fh:
                head = fh.read(512)                      # 头部：录制时间戳
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 900))
                tail = fh.read()                         # 尾部：RealTime
        except OSError:
            missing += 1
            continue
        m = AVF_TIME_RE.search(tail)
        if not m:
            missing += 1
        else:
            rt = float(m.group(1))
            r["realTime"] = rt
            if abs(rt - r["time"]) <= 0.001:
                ok += 1
            else:
                mismatch += 1
        d = AVF_DATE_RE.search(head)
        if d:
            r["date"] = f"{int(d.group(3)):04d}-{int(d.group(2)):02d}-{int(d.group(1)):02d}"
    return {"checked": len(rows), "ok": ok, "mismatch": mismatch, "missing": missing}


def summarize(games, rows):
    out = {}
    for lv in LEVEL_ORDER:
        sub = [r for r in rows if r["level"] == lv]
        gs = [g for g in games if g["level"] == lv]
        if not sub or not gs:
            out[lv] = {"level": lv, "bvCount": 0}
            continue
        fast = min(gs, key=lambda g: (g["time"], g["file"]))
        fast_bvs = max(gs, key=lambda g: (g["bv"] / g["time"], g["file"]))
        out[lv] = {
            "level": lv,
            "bvCount": len(sub),
            "games": len(gs),
            "bvMin": min(r["bv"] for r in sub),
            "bvMax": max(r["bv"] for r in sub),
            "fastTime": round(fast["time"], 2),
            "fastBv": fast["bv"],
            "fastFile": fast["file"],
            "bestBvs": round(fast_bvs["bv"] / fast_bvs["time"], 2),
            "bestBvsBv": fast_bvs["bv"],
            "bestBvsTime": round(fast_bvs["time"], 2),
            "bestBvsFile": fast_bvs["file"],
            "nfGames": sum(1 for g in gs if g["style"] == "NF"),
            "flGames": sum(1 for g in gs if g["style"] == "FL"),
        }
    return out


# ------------------------------------------------------------------ 缓存

class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.payload = None
        self.sig = None
        self.root = HERE

    def get(self, force=False):
        with self.lock:
            root = self.root
            sig = signature(root)
            if force or sig != self.sig or self.payload is None:
                t0 = time.perf_counter()
                games, scan_info = scan(root)
                rows = build_records(games)
                ver = verify_and_date(rows, root)
                self.payload = {
                    "generated": datetime.now().isoformat(timespec="seconds"),
                    "root": root,
                    "elapsedMs": round((time.perf_counter() - t0) * 1000),
                    "signature": sig,
                    "scan": scan_info,
                    "verification": ver,
                    "summary": summarize(games, rows),
                    "records": rows,
                }
                self.sig = sig
            return self.payload


CACHE = Cache()


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "MSBvViewer/1.0"

    def log_message(self, fmt, *args):   # 静音访问日志，只留错误
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            try:
                with open(APP_HTML, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "<h1>app.html 缺失</h1>", "text/html; charset=utf-8")
            return

        if path == "/api/records":
            self._send(200, json.dumps(CACHE.get(), ensure_ascii=False))
            return

        if path == "/api/version":
            p = CACHE.get()
            self._send(200, json.dumps({"signature": p["signature"], "generated": p["generated"]},
                                       ensure_ascii=False))
            return

        if path == "/api/rescan":
            self._send(200, json.dumps(CACHE.get(force=True), ensure_ascii=False))
            return

        if path == "/api/csv":
            p = CACHE.get()
            import io
            buf = io.StringIO()
            cols = ["level", "bv", "games", "best_time", "best_bvs", "best_style", "record_date",
                    "nf_best_time", "fl_best_time", "game_count_nf", "game_count_fl",
                    "best_file", "path"]
            w = csv.writer(buf, lineterminator="\r\n")
            w.writerow(cols)
            for r in p["records"]:
                w.writerow([r["level"], r["bv"], r["games"], r["time"], r["bvs"], r["style"],
                            r["date"] or "", r["nfTime"] if r["nfTime"] is not None else "",
                            r["flTime"] if r["flTime"] is not None else "",
                            r["nfGames"], r["flGames"], r["file"], r["path"]])
            data = ("\ufeff" + buf.getvalue()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="bv_records.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/shutdown":
            self._send(200, '{"ok":true}')
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        self._send(404, '{"error":"not found"}')


def pick_port(preferred=8765):
    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="扫雷 BV 成绩台（本地服务）",
        epilog="不指定 --root 时会自动往上找 replays / replays2 所在的文件夹。")
    ap.add_argument("--root", default=None,
                    help="扫雷根目录（就是装着 replays / replays2 的那个文件夹）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root) if args.root else discover_root(HERE)
    CACHE.root = root
    port = pick_port(args.port)
    url = f"http://127.0.0.1:{port}/"

    print(f"扫描根目录：{root}")

    # 预热：启动前先算一遍，浏览器一打开就有数据
    t0 = time.perf_counter()
    p = CACHE.get(force=True)
    warm = time.perf_counter() - t0
    print(f"已扫描 {p['scan']['files']} 个录像 → {p['scan']['unique']} 局"
          f"（耗时 {warm:.2f}s）")
    for lv in LEVEL_ORDER:
        s = p["summary"][lv]
        if s.get("bvCount"):
            print(f"  {LEVEL_CN[lv]}: {s['bvCount']} 个 BV ({s['bvMin']}-{s['bvMax']})，"
                  f"最快 {s['fastTime']}s @BV={s['fastBv']}，最高 3BV/s {s['bestBvs']}")

    if not p["scan"]["unique"]:
        print("\n  [!] 这个目录下没找到任何 .avf 录像。")
        print("      本程序要和 replays / replays2 放在同一个文件夹里。")
        print("      如果不在，关掉窗口，用参数指定你的扫雷目录：")
        print('          ' + LAUNCHER + ' --root "你的扫雷目录完整路径"')
        print("      （页面照样会打开，指定对了点「重新扫描」即可。）\n")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"\n服务已启动：{url}")
    print("页面里可以随时点「重新扫描」；关掉这个窗口或按 Ctrl+C 即停止。\n")

    if not args.no_open:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        import traceback
        traceback.print_exc()
        # 双击 exe 出错时别让窗口一闪就没，让人看得见原因
        if getattr(sys, "frozen", False):
            try:
                if sys.stdin and sys.stdin.isatty():
                    input("\n出错了。按回车键关闭…")
            except Exception:
                pass
        sys.exit(1)

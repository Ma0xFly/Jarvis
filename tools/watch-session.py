#!/usr/bin/env python3
"""把 Claude Code 的会话 jsonl 渲染成人能读的流水。

用法：
    watch-session.py                 # 最近一条会话，从头到尾打印
    watch-session.py -f              # 同上，并持续跟随（边跑边看）
    watch-session.py 5161dd32        # 按 session id 前缀找
    watch-session.py /path/to.jsonl  # 直接指定文件
    watch-session.py -n 40           # 只看最后 40 条

不依赖任何第三方库。Claude Code 的会话是纯文本 jsonl，
所以"看会话"不需要它的桌面版，tail 一个文件就够。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"


def find_sessions() -> list[Path]:
    return sorted(ROOT.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def resolve(target: str | None) -> Path:
    if target:
        p = Path(target).expanduser()
        if p.is_file():
            return p
        hits = [f for f in find_sessions() if f.stem.startswith(target)]
        if not hits:
            sys.exit(f"找不到会话：{target}")
        return hits[0]
    hits = find_sessions()
    if not hits:
        sys.exit(f"{ROOT} 下没有会话")
    return hits[0]


def brief(s: str, n: int = 160) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render(rec: dict) -> str | None:
    t = rec.get("type")
    if t == "user":
        c = rec.get("message", {}).get("content")
        if isinstance(c, str):
            return f"🧑 {brief(c)}"
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    return f"🧑 {brief(b.get('text', ''))}"
        return None
    if t == "assistant":
        blocks = rec.get("message", {}).get("content") or []
        out = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(f"🤖 {brief(b['text'])}")
            elif b.get("type") == "tool_use":
                name = b.get("name", "?")
                args = b.get("input", {})
                key = next((args[k] for k in ("command", "file_path", "path", "pattern", "url", "query")
                            if isinstance(args.get(k), str)), "")
                out.append(f"   🔧 {name} {brief(key, 100)}")
        return "\n".join(out) if out else None
    if t == "system":
        txt = rec.get("content") or rec.get("subtype") or ""
        return f"— system: {brief(txt, 90)}" if txt else None
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?")
    ap.add_argument("-f", "--follow", action="store_true")
    ap.add_argument("-n", "--tail", type=int, default=0, help="只打印最后 N 条记录")
    args = ap.parse_args()

    path = resolve(args.target)
    print(f"# {path}")
    print(f"# session {path.stem} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(path.stat().st_mtime))}\n")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.tail:
        lines = lines[-args.tail :]

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        out = render(rec)
        if out:
            print(out)

    if not args.follow:
        return

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        print("\n# 跟随中… Ctrl-C 退出")
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.4)
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out = render(rec)
            if out:
                print(out, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
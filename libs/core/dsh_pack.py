"""DSH 整体打包/备份/还原（自包含，不依赖第三方插件）。

分类：
- config        profiles/web 配置 + storages 里的注册表/连接配置
- plugins       自定义插件源码（plugins-src）
- plugins_deps  profile 的 node_modules（体积大，默认关闭）
- skills        自定义 skills
- sessions      DSH 会话日志（体积大，默认关闭）
- attachments   附件（体积大，默认关闭）
- memos         memos 插件数据（默认关闭）
- credentials   桥接凭据（DPAPI 加密文件 + cookie；默认关闭，敏感）
- core          仅安装元数据/版本/插件清单（不复制 node_modules）
- core_full     整个 DSH 安装目录（默认关闭，可能数 GB）

产物：<name>.zip + <name>.zip.sha256（zip 整体哈希）+ 内嵌 manifest.json
（逐文件相对路径/大小/sha256，用于一致性对比与还原校验）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "dsh-backup/1"
DEFAULT_DSH_HOME = Path.home() / ".dsh"
DEFAULT_INSTALL_ROOT = Path.home() / ".dsh" / "install"

CATEGORIES = {
    "config": {
        "label": "配置（profiles/web + storages）",
        "paths": [
            "profiles/web/cordis.yml",
            "profiles/web/cordis.patch.yml",
            "profiles/web/package.json",
            "profiles/web/pnpm-lock.yaml",
            "profiles/web/pnpm-workspace.yaml",
            "storages/workspace.json",
            "storages/mcp_connector.json",
        ],
        "glob": ["storages/*.json"],
        "default": True,
    },
    "plugins": {
        "label": "插件源码（plugins-src）",
        "paths": ["plugins-src"],
        "default": True,
    },
    "plugins_deps": {
        "label": "插件依赖（profiles/web/node_modules）",
        "paths": ["profiles/web/node_modules"],
        "default": False,
        "huge": True,
    },
    "skills": {
        "label": "Skills（skills/）",
        "paths": ["skills"],
        "default": True,
    },
    "sessions": {
        "label": "会话日志（sessions/）",
        "paths": ["sessions"],
        "default": False,
        "huge": True,
    },
    "attachments": {
        "label": "附件（attachments/）",
        "paths": ["attachments"],
        "default": False,
        "huge": True,
    },
    "memos": {
        "label": "Memos 数据（memos-plugin/）",
        "paths": ["memos-plugin"],
        "default": False,
        "huge": True,
    },
    "credentials": {
        "label": "凭据（DPAPI 加密，敏感）",
        "paths": [],
        "state": ["web_launch.json", "web-auth", "http.token"],
        "default": False,
        "sensitive": True,
    },
    "core": {
        "label": "本体元数据（版本/插件清单）",
        "paths": [],
        "install": ["package.json"],
        "default": True,
    },
    "core_full": {
        "label": "本体完整目录（含 node_modules，数 GB）",
        "paths": [],
        "install": ["."],
        "default": False,
        "huge": True,
    },
    "extras": {
        "label": "扩展数据（动态识别：dsh-ops / dsh-windows-mcp / electron 等）",
        "paths": [],
        "dynamic": "home_rest",
        "default": False,
        "huge": True,
    },
    "node_runtime": {
        "label": "Node 运行时（node 安装目录）",
        "paths": [],
        "node": True,
        "default": False,
        "huge": True,
    },
}

EXCLUDED_NAMES = {".git", "__pycache__", ".cache", "cache", "tmp", "node_modules.bak"}
EXCLUDED_SUFFIX = ("-wal", "-shm", ".lock", ".tmp")


def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or DEFAULT_DSH_HOME)


def install_root() -> Path:
    env = os.environ.get("DSH_INSTALL_ROOT")
    if env:
        return Path(env)
    found = shutil.which("dsh")
    if found:
        for parent in Path(found).resolve().parents:
            candidate = parent / "@deepseek-ai" / "dsh"
            if (candidate / "package.json").is_file():
                return candidate
    return DEFAULT_INSTALL_ROOT


def state_dir() -> Path:
    """桥接状态目录（凭据来源），随 DSH_SUBAGENT_STATE 或默认 AppData。"""
    env = os.environ.get("DSH_SUBAGENT_STATE")
    if env:
        return Path(env)
    try:
        from .platform import locations
        return locations().state
    except Exception:  # noqa: BLE001
        return project_root() / "data" / "state"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def backups_dir(output_dir: Path | str | None = None) -> Path:
    """备份保存位置：显式参数 > 环境变量 DSH_BACKUP_DIR > 设置 output_dir > 项目 data/backups。"""
    if output_dir:
        return Path(output_dir)
    env = os.environ.get("DSH_BACKUP_DIR")
    if env:
        return Path(env)
    try:
        configured = (load_settings().get("output_dir") or "").strip()
    except Exception:  # noqa: BLE001
        configured = ""
    if configured:
        return Path(configured)
    return project_root() / "data" / "backups"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _is_link(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return path.is_symlink() or bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(root: Path, arc_prefix: str):
    """只产出普通文件；跳过链接/重解析点、锁文件与缓存目录。"""
    if not root.exists():
        return
    if root.is_file():
        if not _is_link(root) and not root.name.endswith(EXCLUDED_SUFFIX):
            yield root, arc_prefix
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames
            if not (name in EXCLUDED_NAMES) and not _is_link(current / name)
        ]
        for name in filenames:
            path = current / name
            if _is_link(path) or name.endswith(EXCLUDED_SUFFIX):
                continue
            relative = path.relative_to(root).as_posix()
            yield path, arc_prefix + "/" + relative


def extra_categories() -> list[dict]:
    """DSH_HOME 下未被内置分类覆盖的顶层目录 = 插件/扩展数据（动态）。"""
    home = dsh_home()
    if not home.is_dir():
        return []
    covered = set()
    for name, spec in CATEGORIES.items():
        for relative in spec.get("paths", []):
            covered.add(relative.split("/")[0])
    rows = []
    for entry in sorted(os.listdir(home)):
        path = home / entry
        if entry in covered or entry in EXCLUDED_NAMES or not path.is_dir():
            continue
        if _is_link(path):
            continue
        rows.append({"id": "extra:" + entry, "name": entry, "label": "扩展：" + entry,
                     "path": str(path), "default": False, "huge": True, "sensitive": False})
    return rows


def _category_entries(category: str):
    home = dsh_home()
    entries = []
    if category == "extras":
        covered = set()
        for other, other_spec in CATEGORIES.items():
            if other in ("extras", "node_runtime"):
                continue
            for relative in other_spec.get("paths", []):
                covered.add(relative.split("/")[0])
        for name in sorted(os.listdir(home)) if home.is_dir() else []:
            if name in covered or name in EXCLUDED_NAMES:
                continue
            entries.extend(_iter_files(home / name, "extras/" + name))
    if category.startswith("extra:"):
        name = category.split(":", 1)[1]
        if name and name not in EXCLUDED_NAMES and ".." not in name and "/" not in name and "\\" not in name:
            return list(_iter_files(home / name, "extras/" + name))
        return []
    spec = CATEGORIES[category]
    for relative in spec.get("paths", []):
        path = home / relative
        if path.exists():
            normalized = relative.replace("\\", "/")
            prefix = category if normalized == category else (category + "/" + normalized)
            entries.extend(_iter_files(path, prefix))
    for pattern in spec.get("glob", []):
        for path in sorted(home.glob(pattern)):
            if path.is_file() and not _is_link(path):
                entries.extend(_iter_files(path, category + "/" + path.relative_to(home).as_posix()))
    for relative in spec.get("state", []):
        path = state_dir() / relative
        if path.exists():
            entries.extend(_iter_files(path, category + "/state/" + relative.replace("\\", "/")))
    for relative in spec.get("install", []):
        base = install_root() / relative
        if base.exists():
            entries.extend(_iter_files(base, "core/" + install_root().name + "/" + relative.replace("\\", "/")))
    if category.startswith("extra:"):
        name = category.split(":", 1)[1]
        if name and ".." not in name and "/" not in name and "\\" not in name:
            entries.extend(_iter_files(home / name, "extras/" + name))
    if spec.get("node"):
        node = shutil.which("node")
        if node:
            base = Path(node).resolve().parent
            entries.extend(_iter_files(base, "node_runtime/" + base.name))
    return entries


def plan(categories: list[str] | None = None, progress=None) -> dict:
    """返回 {entries:[{src,arc,size,sha256?}], counts, bytes}；sha256 在打包时计算。"""
    chosen = [c for c in (categories or []) if c in CATEGORIES or c.startswith("extra:")]
    if not chosen:
        chosen = [name for name, spec in CATEGORIES.items() if spec.get("default")]
    entries = []
    seen = set()
    for category in chosen:
        for source, arc in _category_entries(category):
            if arc in seen:
                continue
            seen.add(arc)
            try:
                size = source.stat().st_size
            except OSError:
                continue
            entries.append({"category": category, "src": str(source), "arc": arc, "size": size})
            if progress and len(entries) % 200 == 0:
                progress(len(entries), arc)
    counts, sizes = {}, {}
    for item in entries:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
        sizes[item["category"]] = sizes.get(item["category"], 0) + item["size"]
    return {"categories": chosen, "entries": entries,
            "counts": counts, "sizes": sizes,
            "total_files": len(entries), "total_bytes": sum(i["size"] for i in entries)}


_PREVIEW_CACHE: dict[str, tuple[float, dict]] = {}
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_THREADS: dict[str, bool] = {}


def _preview_disk_path() -> Path:
    return state_dir() / "backup-preview.json"


def _load_preview_disk() -> dict:
    path = _preview_disk_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _save_preview_disk(key: str, summary: dict) -> None:
    path = _preview_disk_path()
    data = _load_preview_disk()
    data[key] = {"ts": time.time(), "summary": summary}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _preview_worker(key: str, chosen: list[str]) -> None:
    try:
        value = plan(chosen)
        summary = {
            "categories": value["categories"],
            "counts": value["counts"],
            "sizes": value["sizes"],
            "total_files": value["total_files"],
            "total_bytes": value["total_bytes"],
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with _PREVIEW_LOCK:
            _PREVIEW_CACHE[key] = (time.time(), summary)
        _save_preview_disk(key, summary)
    finally:
        with _PREVIEW_LOCK:
            _PREVIEW_THREADS.pop(key, None)


def preview_state(categories: list[str] | None = None, ttl_seconds: float = 3600) -> dict:
    """非阻塞预估：有缓存立即返回（可过期），无缓存则后台计算并返回 pending。"""
    chosen = list(categories) if categories else list(CATEGORIES.keys())
    chosen = [c for c in chosen if c in CATEGORIES or c.startswith("extra:")]
    key = ",".join(sorted(chosen))
    with _PREVIEW_LOCK:
        cached = _PREVIEW_CACHE.get(key)
        disk = _load_preview_disk().get(key) if not cached else None
    if cached:
        summary = cached[1]
        fresh = (time.time() - cached[0]) < ttl_seconds
    elif disk:
        summary = disk.get("summary") or {}
        fresh = (time.time() - float(disk.get("ts") or 0)) < ttl_seconds
        with _PREVIEW_LOCK:
            _PREVIEW_CACHE[key] = (float(disk.get("ts") or 0), summary)
    else:
        summary, fresh = None, False
    if summary is None:
        with _PREVIEW_LOCK:
            if not _PREVIEW_THREADS.get(key):
                _PREVIEW_THREADS[key] = True
                threading.Thread(target=_preview_worker, args=(key, chosen), daemon=True).start()
        return {"categories": chosen, "sizes": {}, "counts": {}, "total_bytes": 0,
                "total_files": 0, "pending": True}
    if not fresh:
        with _PREVIEW_LOCK:
            if not _PREVIEW_THREADS.get(key):
                _PREVIEW_THREADS[key] = True
                threading.Thread(target=_preview_worker, args=(key, chosen), daemon=True).start()
    return {**summary, "pending": False, "stale": not fresh}


def preview(categories: list[str] | None = None, ttl_seconds: float = 300) -> dict:
    """分类预估（只 stat 不读内容），带 5 分钟缓存。"""
    chosen = list(categories) if categories else list(CATEGORIES.keys())
    key = ",".join(sorted(chosen))
    cached = _PREVIEW_CACHE.get(key)
    if cached and (time.time() - cached[0]) < ttl_seconds:
        return cached[1]
    plan_value = plan(chosen)
    summary = {
        "categories": plan_value["categories"],
        "counts": plan_value["counts"],
        "sizes": plan_value["sizes"],
        "total_files": plan_value["total_files"],
        "total_bytes": plan_value["total_bytes"],
    }
    _PREVIEW_CACHE[key] = (time.time(), summary)
    return summary


def build(categories: list[str] | None = None, name: str | None = None,
          output_dir: Path | None = None, progress=None, scan_progress=None) -> dict:
    """打包为 zip；返回 {name, path, sha256, size, files, categories, created_at}。"""
    selected = plan(categories, progress=scan_progress)
    if not selected["entries"]:
        raise ValueError("没有可打包的文件；请检查 DSH_HOME/分类选择")
    target_dir = Path(output_dir) if output_dir else backups_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = name or ("dsh-" + now_stamp())
    archive = target_dir / (stamp + ".zip")
    if archive.exists():
        raise ValueError("备份文件已存在：" + archive.name)
    manifest_files = []
    written = 0
    written_bytes = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for index, item in enumerate(selected["entries"], 1):
            source = Path(item["src"])
            try:
                digest = _sha256_file(source)
            except OSError:
                continue
            arc = item["arc"]
            if not arc or ".." in arc.replace("\\", "/").split("/") or ":" in arc:
                continue
            bundle.write(source, arc)
            manifest_files.append({"path": arc, "size": item["size"], "sha256": digest})
            written_bytes += item["size"]
            written += 1
            if progress:
                progress(written, selected["total_files"], item["size"], arc, written_bytes, selected["total_bytes"])
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dsh_home": str(dsh_home()),
            "install_root": str(install_root()),
            "categories": selected["categories"],
            "files": manifest_files,
            "environment": environment(),
        }
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        bundle.writestr("PORTABLE.md", portable_notes(manifest))
    archive_hash = _sha256_file(archive)
    (archive.with_name(archive.name + ".sha256")).write_text(
        archive_hash + "  " + archive.name + "\n", encoding="utf-8")
    return {
        "name": archive.name,
        "path": str(archive),
        "sha256": archive_hash,
        "size": archive.stat().st_size,
        "files": len(manifest_files),
        "categories": selected["categories"],
        "created_at": manifest["created_at"],
    }


TEXT_SUFFIXES = (".yml", ".yaml", ".json", ".md", ".txt", ".toml", ".ini", ".cfg", ".js", ".mjs", ".cjs", ".ts", ".sh", ".bat", ".ps1")


def _version_of(path: Path) -> str:
    try:
        data = json.loads((path / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def environment() -> dict:
    import platform
    import subprocess

    try:
        node_version = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        node_version = ""
    return {
        "dsh_version": _version_of(install_root()),
        "node_version": node_version,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dsh_home": str(dsh_home()),
        "install_root": str(install_root()),
    }


def portable_notes(manifest: dict) -> str:
    env = manifest.get("environment") or {}
    return "\n".join([
        "# DSH 可移植备份",
        "",
        "## 目标机器准备",
        "1. 安装同版本 DSH（本体）：" + (env.get("dsh_version") or "未知版本"),
        "2. 安装 Node 运行时：" + (env.get("node_version") or "未知版本"),
        "3. 准备相同的 DSH_HOME 路径，或使用还原时的“目标路径 + 路径重写”。",
        "",
        "## 恢复",
        "- 面板「打包/备份 → 还原」支持填写目标 DSH_HOME 并勾选路径重写；",
        "- 或解压后把各分类目录放回 DSH_HOME / 本体安装目录。",
        "",
        "## 必须重新配置（不可跨机复用）",
        "- 凭据：DPAPI 加密文件只能在原机器解密；请重新登录/生成 token。",
        "- DSH 注册配置里的 Authorization、绝对路径需按新机器调整。",
        "- MCP 子进程/工具链（如 dsh-windows-mcp）如未包含在 extras，请单独安装对应平台版本。",
        "",
        "## 备份环境",
        "```json",
        json.dumps(env, ensure_ascii=False, indent=2),
        "```",
        "",
    ])


def _rewrite_text(data: bytes, replacements: list[tuple[str, str]]) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    for old, new in replacements:
        if old and old != new:
            text = text.replace(old, new)
    return text.encode("utf-8")


def list_backups(output_dir: Path | None = None) -> list[dict]:
    folder = Path(output_dir) if output_dir else backups_dir()
    rows = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob("dsh-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        sidecar = path.with_name(path.name + ".sha256")
        rows.append({
            "name": path.name,
            "size": path.stat().st_size,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "sha256": sidecar.read_text(encoding="utf-8").split()[0] if sidecar.is_file() else "",
        })
    return rows


def _read_manifest(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as bundle:
        return json.loads(bundle.read("manifest.json").decode("utf-8"))


def compare(archive: Path, categories: list[str] | None = None, progress=None) -> dict:
    """对比当前文件与备份 manifest 的 sha256（可选分类）。"""
    manifest = _read_manifest(Path(archive))
    wanted = set(categories or manifest.get("categories") or [])
    home, state = dsh_home(), state_dir()
    same, changed, missing, extra = 0, [], [], []
    known = set()
    selected_files = [item for item in manifest.get("files", [])
                      if item["path"].split("/", 1)[0] in wanted]
    total_files = len(selected_files)
    done = 0
    for item in selected_files:
        arc = item["path"]
        category = arc.split("/", 1)[0]
        known.add(arc)
        done += 1
        if progress:
            progress(done, total_files, arc)
        target = _resolve_loose(arc, home, state)
        if not target or not target.is_file():
            missing.append(arc)
            continue
        actual_hash = _sha256_file(target)
        if actual_hash == item["sha256"]:
            same += 1
        else:
            changed.append({"path": arc, "expected": item["sha256"], "actual": actual_hash})
    # extra：当前源目录里比备份多出来的文件（只扫选中分类，忽略 manifest 内已有）
    for category in wanted:
        if category in CATEGORIES:
            for source, arc in _category_entries(category):
                if arc not in known:
                    extra.append(arc)
    return {"archive": str(archive), "categories": sorted(wanted),
            "same": same, "changed": changed, "missing": missing, "extra": extra,
            "consistent": not (changed or missing or extra)}


def _resolve_loose(arc: str, home: Path, state: Path) -> Path | None:
    """把 manifest 里的逻辑路径解析回当前磁盘路径。"""
    parts = arc.replace("\\", "/").split("/")
    if any(part in ("..", "") or ":" in part for part in parts):
        return None
    if not parts:
        return None
    category, rest = parts[0], parts[1:]
    if category == "credentials":
        if rest and rest[0] == "state":
            return state.joinpath(*rest[1:])
        return None
    if category == "core":
        # arc: core/<install-dir-name>/<relative...>
        return install_root().joinpath(*(rest[1:] if rest else []))
    return home.joinpath(*rest)


def restore(archive: Path, categories: list[str] | None = None,
            confirm: bool = False, dry_run: bool = False,
            safety: bool = True, target_home: str | None = None,
            rewrite_paths: bool = False) -> dict:
    """从备份还原选中分类；默认先对将被覆盖的文件做一次安全备份。

    - 校验每个文件的 sha256 后才写入；
    - 拒绝绝对路径/`..`/盘符；
    - 不删除备份里没有的当前文件（避免误删）；如需完全一致请手动处理。
    """
    if not confirm:
        raise ValueError("还原需要确认（confirm=true）")
    archive = Path(archive)
    manifest = _read_manifest(archive)
    for item in manifest.get("files", []):
        if _resolve_loose(item["path"], dsh_home(), state_dir()) is None:
            raise ValueError("拒绝不安全的备份路径：" + item["path"])
    wanted = set(categories or manifest.get("categories") or [])
    files = [f for f in manifest.get("files", []) if f["path"].split("/", 1)[0] in wanted]
    if not files:
        raise ValueError("该备份没有选中分类的文件")
    home, state = dsh_home(), state_dir()
    restore_home = Path(target_home) if target_home else home
    if target_home and not rewrite_paths:
        # 恢复到新路径时，配置里的绝对路径仍指向旧路径，提示风险
        pass
    replacements = []
    if rewrite_paths:
        replacements = [(str(home), str(restore_home)),
                        (str(install_root()), str(install_root()))]
    targets = []
    for item in files:
        target = _resolve_loose(item["path"], restore_home, state)
        if target is None:
            raise ValueError("拒绝不安全的备份路径：" + item["path"])
        targets.append((item, target))
    if dry_run:
        return {"dry_run": True, "archive": str(archive), "files": len(targets),
                "categories": sorted(wanted),
                "changed": [t[0]["path"] for t in targets if t[1].is_file()],
                "missing": [t[0]["path"] for t in targets if not t[1].is_file()]}
    safety_zip = None
    if safety:
        try:
            safe_name = "pre-restore-" + now_stamp()
            safety_zip = build(sorted(wanted), name=safe_name)["path"]
        except ValueError:
            safety_zip = None
    restored, changed = 0, []
    with zipfile.ZipFile(archive) as bundle:
        for item, target in targets:
            try:
                data = bundle.read(item["path"])
            except KeyError:
                continue
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("备份内容校验失败：" + item["path"])
            if rewrite_paths and target.suffix.lower() in TEXT_SUFFIXES:
                data = _rewrite_text(data, replacements)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.read_bytes() != data:
                changed.append(item["path"])
            target.write_bytes(data)
            restored += 1
    return {"archive": str(archive), "restored": restored,
            "categories": sorted(wanted), "changed": changed,
            "safety_zip": safety_zip,
            "target_home": str(restore_home), "rewrite_paths": bool(rewrite_paths),
            "environment": manifest.get("environment") or {}}


# ---- 定时设置 ----------------------------------------------------------------

SETTINGS_FILE = "backup-settings.json"
STATE_FILE = "backup-state.json"


def load_settings() -> dict:
    path = state_dir() / SETTINGS_FILE
    defaults = {"enabled": False, "interval_hours": 24, "keep": 7,
                "categories": [name for name, spec in CATEGORIES.items() if spec.get("default")],
                "output_dir": ""}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update({k: data[k] for k in list(defaults) if k in data})
        except ValueError:
            pass
    return defaults


def save_settings(settings: dict) -> dict:
    current = load_settings()
    for key in ("enabled", "interval_hours", "keep", "categories", "output_dir"):
        if key in settings:
            current[key] = settings[key]
    current["enabled"] = bool(current.get("enabled"))
    current["interval_hours"] = max(1, min(24 * 30, int(current.get("interval_hours") or 24)))
    current["keep"] = max(1, min(100, int(current.get("keep") or 7)))
    current["categories"] = [c for c in (current.get("categories") or []) if c in CATEGORIES] or [
        name for name, spec in CATEGORIES.items() if spec.get("default")]
    path = state_dir() / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
    return current


def load_state() -> dict:
    path = state_dir() / STATE_FILE
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def _save_state(state: dict) -> None:
    path = state_dir() / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def due(settings: dict | None = None) -> bool:
    settings = settings or load_settings()
    if not settings.get("enabled"):
        return False
    last = load_state().get("last_run")
    interval = float(settings.get("interval_hours") or 24) * 3600
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() >= interval
    except ValueError:
        return True


def mark_run(result: dict | None) -> None:
    state = load_state()
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if result:
        state["last_backup"] = result.get("name")
        state["last_sha256"] = result.get("sha256")
    _save_state(state)


def prune(settings: dict | None = None) -> list[str]:
    settings = settings or load_settings()
    keep = int(settings.get("keep") or 7)
    rows = list_backups()
    removed = []
    for row in rows[keep:]:
        path = backups_dir() / row["name"]
        for candidate in (path, path.with_name(path.name + ".sha256")):
            try:
                if candidate.is_file():
                    candidate.unlink()
                    removed.append(candidate.name)
            except OSError:
                pass
    return removed


def rotate(settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    if not settings.get("enabled"):
        return {"ran": False, "reason": "disabled"}
    if not due(settings):
        return {"ran": False, "reason": "not-due"}
    result = build(settings.get("categories"))
    mark_run(result)
    result["pruned"] = prune(settings)
    return {"ran": True, "result": result}
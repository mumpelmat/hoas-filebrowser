import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict

from flask import Flask, Response, jsonify, request, send_file
from werkzeug.utils import secure_filename

APP_PORT = int(os.environ.get("PORT", "8099"))
OPTIONS_FILE = "/data/options.json"

ROOTS: Dict[str, Path] = {
    "config": Path("/config"),
    "share": Path("/share"),
    "media": Path("/media"),
    "ssl": Path("/ssl"),
    "addons": Path("/addons"),
    "backup": Path("/backup"),
}

DEFAULT_OPTIONS = {
    "allowed_roots": list(ROOTS.keys()),
    "max_upload_mb": 500,
}

app = Flask(__name__)


def load_options():
    try:
        with open(OPTIONS_FILE, "r", encoding="utf-8") as f:
            opts = json.load(f)
            return {**DEFAULT_OPTIONS, **opts}
    except Exception:
        return DEFAULT_OPTIONS


def allowed_roots():
    opts = load_options()
    return [r for r in opts.get("allowed_roots", []) if r in ROOTS]


def safe_path(root_name: str, rel_path: str = "") -> Path:
    if root_name not in allowed_roots():
        raise ValueError("Root is not allowed")

    base = ROOTS[root_name].resolve()
    target = (base / rel_path.lstrip("/")).resolve()

    if target != base and base not in target.parents:
        raise ValueError("Path escapes allowed root")

    return target


def file_info(path: Path, base: Path):
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path.relative_to(base)),
        "is_dir": path.is_dir(),
        "size": stat.st_size if path.is_file() else None,
        "modified": int(stat.st_mtime),
    }


def sanitize_uploaded_path(filename: str) -> Path:
    parts = []
    for part in filename.replace("\\", "/").split("/"):
        safe_part = secure_filename(part)
        if safe_part:
            parts.append(safe_part)

    if not parts:
        raise ValueError("Invalid filename")

    return Path(*parts)


@app.errorhandler(Exception)
def handle_error(e):
    return jsonify({"error": str(e)}), 400


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/roots")
def api_roots():
    roots = []
    for name in allowed_roots():
        p = ROOTS[name]
        roots.append({"name": name, "exists": p.exists()})
    return jsonify({"roots": roots})


@app.route("/api/list")
def api_list():
    root = request.args.get("root", "config")
    rel = request.args.get("path", "")
    base = ROOTS[root].resolve()
    current = safe_path(root, rel)
    current.mkdir(parents=True, exist_ok=True)

    dirs = []
    files = []
    for entry in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if entry.name.startswith("."):
            continue
        info = file_info(entry, base)
        if entry.is_dir():
            dirs.append(info)
        else:
            files.append(info)

    parent = ""
    if current != base:
        parent = str(current.parent.relative_to(base))

    return jsonify({
        "root": root,
        "path": str(current.relative_to(base)),
        "parent": parent,
        "items": dirs + files,
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    opts = load_options()
    max_bytes = int(opts.get("max_upload_mb", 500)) * 1024 * 1024
    if request.content_length and request.content_length > max_bytes:
        raise ValueError("Upload exceeds max_upload_mb")

    root = request.form.get("root", "config")
    rel = request.form.get("path", "")
    dest_dir = safe_path(root, rel)
    dest_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    for f in request.files.getlist("files"):
        filename = f.filename or ""
        upload_path = sanitize_uploaded_path(filename)
        dest = safe_path(root, str(Path(rel) / upload_path))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not upload_path.name:
            continue
        f.save(dest)
        uploaded.append(str(upload_path))

    return jsonify({"uploaded": uploaded})


@app.route("/api/download")
def api_download():
    root = request.args.get("root", "config")
    rel = request.args.get("path", "")
    target = safe_path(root, rel)

    if target.is_file():
        return send_file(target, as_attachment=True, download_name=target.name)

    if target.is_dir():
        zip_path = Path("/tmp") / f"{target.name or root}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for file in target.rglob("*"):
                if file.is_file():
                    z.write(file, file.relative_to(target.parent))
        return send_file(zip_path, as_attachment=True, download_name=zip_path.name)

    raise ValueError("Path not found")


@app.route("/api/mkdir", methods=["POST"])
def api_mkdir():
    data = request.json or {}
    root = data.get("root", "config")
    rel = data.get("path", "")
    name = secure_filename(data.get("name", ""))
    if not name:
        raise ValueError("Folder name required")
    safe_path(root, str(Path(rel) / name)).mkdir(parents=True, exist_ok=False)
    return jsonify({"ok": True})


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.json or {}
    root = data.get("root", "config")
    rel = data.get("path", "")
    new_name = secure_filename(data.get("new_name", ""))
    if not new_name:
        raise ValueError("New name required")
    source = safe_path(root, rel)
    target = safe_path(root, str(source.parent.relative_to(ROOTS[root].resolve()) / new_name))
    source.rename(target)
    return jsonify({"ok": True})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.json or {}
    root = data.get("root", "config")
    rel = data.get("path", "")
    target = safe_path(root, rel)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.is_file():
        target.unlink()
    else:
        raise ValueError("Path not found")
    return jsonify({"ok": True})


@app.route("/api/copy", methods=["POST"])
def api_copy():
    data = request.json or {}
    src_root = data.get("src_root", "config")
    src_path = data.get("src_path", "")
    dst_root = data.get("dst_root", src_root)
    dst_path = data.get("dst_path", "")
    src = safe_path(src_root, src_path)
    dst_dir = safe_path(dst_root, dst_path)
    dst = dst_dir / src.name
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    elif src.is_file():
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    else:
        raise ValueError("Source not found")
    return jsonify({"ok": True})


@app.route("/api/move", methods=["POST"])
def api_move():
    data = request.json or {}
    src_root = data.get("src_root", "config")
    src_path = data.get("src_path", "")
    dst_root = data.get("dst_root", src_root)
    dst_path = data.get("dst_path", "")
    src = safe_path(src_root, src_path)
    dst_dir = safe_path(dst_root, dst_path)
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst_dir / src.name))
    return jsonify({"ok": True})


INDEX_HTML = r'''
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local File Browser</title>
  <style>
        :root { --bg:#f6f7f9; --card:#fff; --text:#17202a; --muted:#6b7280; --line:#d9dee7; --accent:#2563eb; --danger:#b91c1c; --hover:#eef4ff; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:var(--bg); color:var(--text); }
    header { padding:14px 18px; background:var(--card); border-bottom:1px solid var(--line); display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    h1 { font-size:18px; margin:0 10px 0 0; }
        select, button, input { font:inherit; border:1px solid var(--line); border-radius:8px; padding:8px 10px; background:#fff; }
    button { cursor:pointer; }
    button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
    button.danger { color:var(--danger); }
    main { padding:18px; }
        .bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
    .path { color:var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
    table { width:100%; border-collapse:collapse; }
        th, td { padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; }
    th { background:#fafafa; color:var(--muted); font-weight:600; }
        tr:hover td { background:#f9fbff; }
        tr.selected td { background:var(--hover); }
        .name { cursor:pointer; font-weight:600; }
        .toolbar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; padding:10px; background:var(--card); border:1px solid var(--line); border-radius:12px; }
        .toolbar .count { color:var(--muted); font-size:13px; margin-right:auto; }
        .icon-btn { display:inline-flex; align-items:center; gap:6px; }
        .icon { width:1em; height:1em; display:inline-block; line-height:1; }
        .checkbox-col { width:42px; }
    .drop { border:2px dashed var(--line); border-radius:12px; padding:18px; text-align:center; color:var(--muted); margin-bottom:12px; background:#fff; }
    .drop.drag { border-color:var(--accent); color:var(--accent); }
    .small { font-size:12px; color:var(--muted); }
        .context-menu { position:fixed; z-index:1000; min-width:220px; background:var(--card); border:1px solid var(--line); border-radius:12px; box-shadow:0 16px 40px rgba(15,23,42,.14); padding:6px; display:none; }
        .context-menu button { width:100%; justify-content:flex-start; border:none; background:transparent; padding:10px 12px; border-radius:10px; display:flex; align-items:center; gap:8px; }
        .context-menu button:hover { background:#f3f7ff; }
        .context-menu button.danger { color:var(--danger); }
        @media(max-width:800px){ .hide-mobile{display:none} th,td{padding:8px} .toolbar{gap:6px} }
  </style>
</head>
<body>
<header>
  <h1>Files</h1>
  <select id="root"></select>
    <button class="icon-btn" onclick="goUp()"><span class="icon">⬆️</span><span>Hoch</span></button>
    <button class="icon-btn" onclick="newFolder()"><span class="icon">📁</span><span>Neuer Ordner</span></button>
    <label><input id="fileInput" type="file" multiple hidden onchange="uploadFiles(this.files)"><button class="primary" onclick="document.getElementById('fileInput').click()">Dateien hochladen</button></label>
    <label><input id="folderInput" type="file" webkitdirectory directory multiple hidden onchange="uploadFiles(this.files)"><button class="primary" onclick="document.getElementById('folderInput').click()">Ordner hochladen</button></label>
</header>
<main>
  <div class="bar"><span>Pfad:</span><span id="path" class="path"></span></div>
    <div class="toolbar">
        <div id="selectionCount" class="count">0 ausgewählt</div>
        <button class="icon-btn" id="downloadBtn" onclick="downloadSelection()"><span class="icon">⬇️</span><span>Download</span></button>
        <button class="icon-btn" id="renameBtn" onclick="renameSelection()"><span class="icon">✏️</span><span>Umbenennen</span></button>
        <button class="icon-btn" id="copyBtn" onclick="copySelection()"><span class="icon">📋</span><span>Kopieren</span></button>
        <button class="icon-btn" id="moveBtn" onclick="moveSelection()"><span class="icon">📦</span><span>Verschieben</span></button>
        <button class="icon-btn danger" id="deleteBtn" onclick="deleteSelection()"><span class="icon">🗑️</span><span>Löschen</span></button>
        <button class="icon-btn" onclick="clearSelection()"><span class="icon">✖️</span><span>Auswahl löschen</span></button>
    </div>
    <div id="drop" class="drop">Dateien oder Ordner hier hineinziehen zum Upload in den aktuellen Ordner</div>
  <div class="card">
    <table>
            <thead><tr><th class="checkbox-col"></th><th>Name</th><th class="hide-mobile">Typ</th><th class="hide-mobile">Größe</th></tr></thead>
      <tbody id="items"></tbody>
    </table>
  </div>
  <p class="small">Achtung: Änderungen wirken direkt auf die gemounteten Home-Assistant-Ordner.</p>
</main>
<div id="contextMenu" class="context-menu" role="menu" aria-hidden="true">
    <button type="button" onclick="contextAction('download')"><span class="icon">⬇️</span>Download</button>
    <button type="button" onclick="contextAction('rename')"><span class="icon">✏️</span>Umbenennen</button>
    <button type="button" onclick="contextAction('copy')"><span class="icon">📋</span>Kopieren</button>
    <button type="button" onclick="contextAction('move')"><span class="icon">📦</span>Verschieben</button>
    <button type="button" class="danger" onclick="contextAction('delete')"><span class="icon">🗑️</span>Löschen</button>
</div>
<script>
let currentRoot = 'config';
let currentPath = '';
let parentPath = '';
let selectedPaths = new Set();
let selectedItem = null;
let contextPath = '';

async function api(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let msg = 'Fehler';
    try { msg = (await res.json()).error || msg; } catch(e) {}
    throw new Error(msg);
  }
  return res.json();
}

function enc(v){ return encodeURIComponent(v || ''); }
function size(n){ if(n===null||n===undefined) return ''; const u=['B','KB','MB','GB']; let i=0; while(n>1024&&i<u.length-1){n/=1024;i++;} return `${n.toFixed(i?1:0)} ${u[i]}`; }
function icon(name){ return `<span class="icon" aria-hidden="true">${name}</span>`; }

function selectedItems() {
    return [...selectedPaths];
}

function refreshToolbar() {
    const count = selectedPaths.size;
    document.getElementById('selectionCount').textContent = `${count} ausgewählt`;
    document.getElementById('downloadBtn').disabled = count === 0 || count > 1;
    document.getElementById('renameBtn').disabled = count !== 1;
    document.getElementById('copyBtn').disabled = count === 0;
    document.getElementById('moveBtn').disabled = count === 0;
    document.getElementById('deleteBtn').disabled = count === 0;
}

function clearSelection() {
    selectedPaths.clear();
    selectedItem = null;
    refreshToolbar();
    document.querySelectorAll('input[data-path]').forEach(cb => { cb.checked = false; });
    document.querySelectorAll('tr[data-path]').forEach(tr => tr.classList.remove('selected'));
}

function setSelection(path, checked) {
    if (checked) {
        selectedPaths.add(path);
    } else {
        selectedPaths.delete(path);
    }
    const row = document.querySelector(`tr[data-path="${CSS.escape(path)}"]`);
    if (row) row.classList.toggle('selected', checked);
    refreshToolbar();
}

function toggleSelection(path, checked) {
    setSelection(path, checked);
}

function selectedOrContext() {
    if (selectedPaths.size) return [...selectedPaths];
    return contextPath ? [contextPath] : [];
}

function hideContextMenu() {
    const menu = document.getElementById('contextMenu');
    menu.style.display = 'none';
    menu.setAttribute('aria-hidden', 'true');
}

function showContextMenu(x, y, path, isDir) {
    contextPath = path;
    selectedItem = {path, isDir};
    const menu = document.getElementById('contextMenu');
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
    menu.style.display = 'block';
    menu.setAttribute('aria-hidden', 'false');
}

document.addEventListener('click', hideContextMenu);
document.addEventListener('scroll', hideContextMenu, true);
document.addEventListener('contextmenu', e => {
    if (!e.target.closest('#items')) hideContextMenu();
});

async function loadRoots() {
  const data = await api('api/roots');
  const select = document.getElementById('root');
  select.innerHTML = '';
  data.roots.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.name;
    opt.textContent = r.name;
    select.appendChild(opt);
  });
  select.value = currentRoot;
  select.onchange = () => { currentRoot = select.value; currentPath = ''; loadList(); };
}

async function loadList() {
  const data = await api(`api/list?root=${enc(currentRoot)}&path=${enc(currentPath)}`);
  currentPath = data.path || '';
  parentPath = data.parent || '';
    clearSelection();
  document.getElementById('path').textContent = `/${currentRoot}/${currentPath}`.replace(/\/$/, '');
  const tbody = document.getElementById('items');
  tbody.innerHTML = '';
  data.items.forEach(item => {
    const tr = document.createElement('tr');
        tr.dataset.path = item.path;
        tr.dataset.isDir = item.is_dir ? '1' : '0';
        const rowIcon = item.is_dir ? '📁' : '📄';
    tr.innerHTML = `
            <td class="checkbox-col"><input type="checkbox" data-path="${escapeJs(item.path)}"></td>
            <td class="name">${rowIcon} ${item.name}</td>
      <td class="hide-mobile">${item.is_dir ? 'Ordner' : 'Datei'}</td>
            <td class="hide-mobile">${item.is_dir ? '' : size(item.size)}</td>`;
        const checkbox = tr.querySelector('input[type="checkbox"]');
        checkbox.addEventListener('click', e => e.stopPropagation());
        checkbox.addEventListener('change', () => toggleSelection(item.path, checkbox.checked));
        tr.querySelector('.name').onclick = () => {
            hideContextMenu();
            if(item.is_dir){ currentPath = item.path; loadList(); } else { downloadItem(item.path); }
        };
        tr.oncontextmenu = e => {
            e.preventDefault();
            showContextMenu(e.clientX, e.clientY, item.path, item.is_dir);
        };
    tbody.appendChild(tr);
  });
    refreshToolbar();
}

function escapeJs(s){ return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'"); }
function goUp(){ currentPath = parentPath || ''; loadList(); }
function downloadItem(path){ window.location = `api/download?root=${enc(currentRoot)}&path=${enc(path)}`; }

function currentSelectionPath() {
    if (selectedPaths.size === 1) return [...selectedPaths][0];
    if (contextPath) return contextPath;
    return null;
}

function ensureSingleSelection(action) {
    const path = currentSelectionPath();
    if (!path) {
        alert(`Bitte zuerst ein Element für ${action} auswählen.`);
        return null;
    }
    if (selectedPaths.size > 1) {
        alert(`Für ${action} bitte nur ein Element auswählen.`);
        return null;
    }
    return path;
}

async function uploadFiles(files) {
  const fd = new FormData();
  fd.append('root', currentRoot);
  fd.append('path', currentPath);
    [...files].forEach(f => {
        const relativePath = f.webkitRelativePath || f.name;
        fd.append('files', f, relativePath);
    });
  await api('api/upload', {method:'POST', body:fd});
  await loadList();
}

function readDirectoryEntries(reader) {
    return new Promise((resolve, reject) => {
        const entries = [];
        const readBatch = () => {
            reader.readEntries(batch => {
                if (!batch.length) {
                    resolve(entries);
                    return;
                }
                entries.push(...batch);
                readBatch();
            }, reject);
        };
        readBatch();
    });
}

async function collectDroppedFiles(items) {
    const files = [];

    async function walkEntry(entry, prefix = '') {
        if (entry.isFile) {
            const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
            files.push({file, relativePath: `${prefix}${entry.name}`});
            return;
        }

        if (entry.isDirectory) {
            const children = await readDirectoryEntries(entry.createReader());
            for (const child of children) {
                await walkEntry(child, `${prefix}${entry.name}/`);
            }
        }
    }

    for (const item of items) {
        const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
        if (entry) {
            await walkEntry(entry);
            continue;
        }

        const file = item.getAsFile ? item.getAsFile() : item;
        if (file) {
            files.push({file, relativePath: file.webkitRelativePath || file.name});
        }
    }

    return files;
}

async function uploadDroppedItems(items) {
    const files = await collectDroppedFiles(items);
    if (!files.length) return;
    const fd = new FormData();
    fd.append('root', currentRoot);
    fd.append('path', currentPath);
    files.forEach(({file, relativePath}) => fd.append('files', file, relativePath));
    await api('api/upload', {method:'POST', body:fd});
    await loadList();
}

async function newFolder(){
  const name = prompt('Ordnername');
  if(!name) return;
  await api('api/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({root:currentRoot,path:currentPath,name})});
  loadList();
}

async function renameItem(path, oldName){
  const new_name = prompt('Neuer Name', oldName);
  if(!new_name || new_name === oldName) return;
  await api('api/rename', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({root:currentRoot,path,new_name})});
  loadList();
}

async function deleteItem(path){
  if(!confirm(`${path} wirklich löschen?`)) return;
  await api('api/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({root:currentRoot,path})});
  loadList();
}

async function copyItem(src_path){
  const dst_path = prompt('Zielordner relativ zum aktuellen Root', currentPath);
  if(dst_path === null) return;
  await api('api/copy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({src_root:currentRoot,src_path,dst_root:currentRoot,dst_path})});
  loadList();
}

async function moveItem(src_path){
  const dst_path = prompt('Zielordner relativ zum aktuellen Root', currentPath);
  if(dst_path === null) return;
  await api('api/move', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({src_root:currentRoot,src_path,dst_root:currentRoot,dst_path})});
  loadList();
}

async function downloadSelection() {
    const path = ensureSingleSelection('Download');
    if (!path) return;
    downloadItem(path);
}

async function renameSelection() {
    const path = ensureSingleSelection('Umbenennen');
    if (!path) return;
    const item = document.querySelector(`tr[data-path="${CSS.escape(path)}"]`);
    const oldName = item ? item.querySelector('.name').textContent.replace(/^[📁📄]\s*/, '').trim() : path.split('/').pop();
    await renameItem(path, oldName);
}

async function copySelection() {
    const items = selectedOrContext();
    if (!items.length) return;
    const dst_path = prompt('Zielordner relativ zum aktuellen Root', currentPath);
    if(dst_path === null) return;
    for (const src_path of items) {
        await api('api/copy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({src_root:currentRoot,src_path,dst_root:currentRoot,dst_path})});
    }
    loadList();
}

async function moveSelection() {
    const items = selectedOrContext();
    if (!items.length) return;
    const dst_path = prompt('Zielordner relativ zum aktuellen Root', currentPath);
    if(dst_path === null) return;
    for (const src_path of items) {
        await api('api/move', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({src_root:currentRoot,src_path,dst_root:currentRoot,dst_path})});
    }
    loadList();
}

async function deleteSelection() {
    const items = selectedOrContext();
    if (!items.length) return;
    if(!confirm(`${items.length} Element(e) wirklich löschen?`)) return;
    for (const path of items) {
        await api('api/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({root:currentRoot,path})});
    }
    loadList();
}

async function contextAction(action) {
    hideContextMenu();
    const path = currentSelectionPath() || contextPath;
    if (!path) return;
    if (!selectedPaths.size) selectedPaths.add(path);
    if (action === 'download') return downloadSelection();
    if (action === 'rename') return renameSelection();
    if (action === 'copy') return copySelection();
    if (action === 'move') return moveSelection();
    if (action === 'delete') return deleteSelection();
}

const drop = document.getElementById('drop');
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag'); });
drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
drop.addEventListener('drop', async e => {
    e.preventDefault();
    drop.classList.remove('drag');
    await uploadDroppedItems(e.dataTransfer.items || e.dataTransfer.files);
});

loadRoots().then(loadList).catch(e => alert(e.message));
</script>
</body>
</html>
'''


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)

import os, sys, json
from datetime import datetime

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".heic", ".tiff", ".tif", ".raw", ".cr2"}
PDF_EXTS = {".pdf"}

def format_size(sz):
    for unit in ("B", "KB", "MB", "GB"):
        if sz < 1024: return f"{sz:.1f} {unit}"
        sz /= 1024
    return f"{sz:.1f} TB"

def search(base_dir, keyword, file_type, max_results=200):
    results = []
    base_depth = base_dir.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base_dir):
        if len(results) >= max_results:
            break
        depth = root.count(os.sep) - base_depth
        if depth > 5:
            dirs.clear()
            continue
        if file_type in ("folder", "all"):
            for d in dirs:
                if len(results) >= max_results: break
                if keyword in d.lower():
                    fp = os.path.join(root, d)
                    st = os.stat(fp)
                    results.append({"name": d, "path": fp, "type": "folder", "size": "—", "size_bytes": 0,
                        "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        if file_type != "folder":
            for f in files:
                if len(results) >= max_results: break
                ext = os.path.splitext(f)[1].lower()
                if file_type == "image" and ext not in IMAGE_EXTS: continue
                if file_type == "pdf" and ext not in PDF_EXTS: continue
                if file_type == "all" and ext not in IMAGE_EXTS and ext not in PDF_EXTS: continue
                if keyword in f.lower():
                    fp = os.path.join(root, f)
                    st = os.stat(fp)
                    ft = "image" if ext in IMAGE_EXTS else "pdf"
                    results.append({"name": f, "path": fp, "type": ft, "size": format_size(st.st_size),
                        "size_bytes": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return {"results": results[:max_results], "total": len(results)}

if __name__ == "__main__":
    d = json.loads(sys.stdin.read())
    r = search(d["dir"], d["q"], d["type"])
    print(json.dumps(r, ensure_ascii=False))

import os, re, uuid, datetime, imghdr
from flask import Flask, request, jsonify, abort, Response, send_file
from werkzeug.utils import secure_filename
from mimetypes import guess_type

# Opcional: pip install flask-cors
try:
    from flask_cors import CORS
except ImportError:
    CORS = None

app = Flask(__name__)

MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/app/media")
ALLOWED_EXT = set((os.getenv("ALLOWED_MEDIA_EXT", "mp4,webm,mov,m4v,avi,jpg,jpeg,png,webp").lower()).split(","))
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "400"))
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")  # Authorization: Bearer <token>
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://storage.grupoupper.com.br")  # ex: https://storage.seudominio.com.br
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

if CORS:
    CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def _auth_ok(req) -> bool:
    if not UPLOAD_TOKEN:
        return True  # sem token => sem auth (não recomendado em produção)
    auth = req.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth.split(" ", 1)[1] == UPLOAD_TOKEN

@app.route("/health")
def health():
    return jsonify(ok=True)

@app.route("/admin/media/upload", methods=["POST"])
def media_upload():
    if not _auth_ok(request):
        return jsonify(ok=False, error="unauthorized"), 401

    f = request.files.get("file")
    if not f or f.filename == "":
        return jsonify(ok=False, error="file missing"), 400

    if not _allowed_file(f.filename):
        return jsonify(ok=False, error="file extension not allowed"), 400

    ext = f.filename.rsplit(".", 1)[1].lower()
    today = datetime.datetime.utcnow()
    subdir = os.path.join(MEDIA_ROOT, "uploads", today.strftime("%Y"), today.strftime("%m"))
    os.makedirs(subdir, exist_ok=True)

    base = secure_filename(os.path.splitext(f.filename)[0])[:80] or "file"
    filename = f"{base}-{uuid.uuid4().hex[:8]}.{ext}"
    fullpath = os.path.join(subdir, filename)
    f.save(fullpath)

    # Validação mínima de imagem (protege upload trocado de extensão)
    if ext in {"jpg", "jpeg", "png", "webp"}:
        try:
            if ext == "webp":
                # imghdr não detecta webp; deixa passar
                pass
            else:
                detected = imghdr.what(fullpath)
                if detected is None:
                    return jsonify(ok=False, error="invalid image"), 400
        except Exception:
            return jsonify(ok=False, error="invalid image"), 400

    rel_url = f"/cdn/uploads/{today.strftime('%Y')}/{today.strftime('%m')}/{filename}"
    abs_url = f"{PUBLIC_BASE_URL}{rel_url}"
    mime = guess_type(fullpath)[0] or "application/octet-stream"
    size = os.path.getsize(fullpath)

    return jsonify(ok=True, url=abs_url, rel_url=rel_url, mime=mime, size=size)

def _file_iter(path, start=0, end=None, chunk=8192):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = None if end is None else end - start + 1
        while True:
            if remaining is not None and remaining <= 0:
                break
            read = chunk if remaining is None else min(chunk, remaining)
            data = f.read(read)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            yield data

@app.route("/cdn/<path:relpath>")
def cdn(relpath):
    # evita path traversal
    safe_rel = os.path.normpath(relpath)
    full = os.path.join(MEDIA_ROOT, safe_rel)
    full = os.path.abspath(full)
    root_abs = os.path.abspath(MEDIA_ROOT)
    if not full.startswith(root_abs + os.sep):
        abort(403)

    if not os.path.isfile(full):
        abort(404)

    mime = guess_type(full)[0] or "application/octet-stream"
    file_size = os.path.getsize(full)

    rng = request.headers.get("Range", "").strip()
    m = re.match(r"bytes=(\d+)-(\d*)", rng) if rng else None
    if m:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
        if start >= file_size:
            return Response(status=416)
        headers = {
            "Content-Type": mime,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "public, max-age=31536000",
            "Access-Control-Allow-Origin": ",".join(ALLOWED_ORIGINS) if ALLOWED_ORIGINS != ["*"] else "*",
        }
        return Response(_file_iter(full, start, end), status=206, headers=headers)

    rv = send_file(full, mimetype=mime, conditional=True, as_attachment=False, max_age=31536000)
    rv.headers["Accept-Ranges"] = "bytes"
    rv.headers["Cache-Control"] = "public, max-age=31536000"
    rv.headers["Access-Control-Allow-Origin"] = ",".join(ALLOWED_ORIGINS) if ALLOWED_ORIGINS != ["*"] else "*"
    return rv

def _to_rel_url(url_or_rel: str) -> str:
    """
    Aceita tanto a URL completa (https://storage.../cdn/...) quanto o rel_url (/cdn/...).
    Retorna SEMPRE o rel_url iniciado por /cdn/...
    """
    s = url_or_rel.strip()
    if s.startswith("http://") or s.startswith("https://"):
        # remove PUBLIC_BASE_URL do começo
        base = PUBLIC_BASE_URL.rstrip("/")
        if s.startswith(base):
            s = s[len(base):]
    if not s.startswith("/cdn/"):
        raise ValueError("path deve começar com /cdn/")
    return s

def _rel_to_full(rel_url: str) -> str:
    rel = rel_url.lstrip("/")
    full = os.path.join(MEDIA_ROOT, os.path.normpath(rel))
    # trava em MEDIA_ROOT
    full_abs = os.path.abspath(full)
    root_abs = os.path.abspath(MEDIA_ROOT)
    if not full_abs.startswith(root_abs + os.sep):
        raise PermissionError("path traversal")
    return full_abs

@app.route("/admin/ping", methods=["GET"])
def admin_ping():
    if not _auth_ok(request):
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(ok=True, msg="pong")

@app.route("/admin/media/delete", methods=["POST", "DELETE", "GET"])
def media_delete():
    # Auth (mesmo do upload)
    if not _auth_ok(request):
        return jsonify(ok=False, error="unauthorized"), 401

    # Tenta JSON, depois form, depois querystring
    data = request.get_json(silent=True) or {}
    if not data:
        # aceita multipart/form-data
        if request.form:
            data = request.form.to_dict()
        elif request.args:
            data = request.args.to_dict()
        else:
            data = {}

    url_or_rel = (data.get("url") or data.get("rel_url") or "").strip()
    if not url_or_rel:
        return jsonify(ok=False, error="missing url or rel_url", received=data), 400

    try:
        # aceita URL completa também
        s = url_or_rel
        if s.startswith("http://") or s.startswith("https://"):
            base = PUBLIC_BASE_URL.rstrip("/")
            if s.startswith(base):
                s = s[len(base):]
        if not s.startswith("/cdn/"):
            return jsonify(ok=False, error="path must start with /cdn/", got=s), 400

        # normaliza e evita traversal
        rel = s.lstrip("/")
        full = os.path.join(MEDIA_ROOT, os.path.normpath(rel))
        full_abs = os.path.abspath(full)
        root_abs = os.path.abspath(MEDIA_ROOT)
        if not full_abs.startswith(root_abs + os.sep):
            return jsonify(ok=False, error="forbidden path"), 403

        if os.path.isfile(full_abs):
            os.remove(full_abs)
            # limpa diretórios vazios (uploads/AAAA/MM)
            try:
                parent = os.path.dirname(full_abs)
                for _ in range(3):
                    if os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                        parent = os.path.dirname(parent)
                    else:
                        break
            except Exception:
                pass

            return jsonify(ok=True, deleted="/" + rel)
        else:
            return jsonify(ok=False, error="file not found", rel_url="/" + rel), 404

    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

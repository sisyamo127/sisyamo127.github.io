"""おもちゃブログ記事生成の管理用ローカルWebアプリ。

起動方法:
  python scripts/webapp/app.py

起動後、ブラウザで http://localhost:5000 を開く。
ページ構成(1ページ1機能):
  /                          ホーム(概要・最近の記事)
  /generate                  テーマ入力 → タイトル案生成
  /generate/titles/<job_id>  タイトル案 + SEOチェック結果を確認・選択
  /generate/outline/<job_id> アウトライン(構成案)を確認・承認
  /jobs/<job_id>             本文生成(最終ステップ)の進捗・結果
  /history                   生成履歴の一覧・検索
  /rewrite                   既存記事の選択 → リライト
  /rewrite/preview/<job_id>  リライト結果のプレビュー・上書き承認
  /status                    API連携状況の確認
"""

import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta

from flask import Flask, abort, redirect, render_template, request, url_for

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import generate_article as ga  # noqa: E402

app = Flask(__name__)

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
_jobs = {}
_jobs_lock = threading.Lock()


def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_history_entry(entry: dict) -> None:
    history = load_history()
    history.insert(0, entry)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def start_job(fn) -> str:
    """バックグラウンドスレッドでfn(log)を実行し、job_idを返す。

    fnはlogコールバックを受け取り、結果(dict)を返す関数。
    進捗・結果は/jobs/<job_id>/dataで取得できる。
    """
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "logs": [], "result": None, "error": None}

    def log(message: str) -> None:
        with _jobs_lock:
            _jobs[job_id]["logs"].append(message)

    def runner():
        try:
            result = fn(log)
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = result
        except Exception as exc:
            log(f"エラーが発生しました: {exc}")
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(exc)

    threading.Thread(target=runner, daemon=True).start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def get_categories() -> list:
    try:
        return sorted(c["name"] for c in ga.fetch_categories() if c["name"] != "Uncategorized")
    except Exception:
        return []


@app.route("/")
def dashboard():
    history = load_history()
    week_ago = datetime.now() - timedelta(days=7)
    this_week_count = sum(
        1 for h in history
        if datetime.strptime(h["created_at"], "%Y-%m-%d %H:%M:%S") >= week_ago
    )
    success_count = sum(1 for h in history if h.get("wp_link"))
    error_count = sum(1 for h in history if not h.get("wp_link"))
    return render_template(
        "dashboard.html",
        active="home",
        history=history,
        this_week_count=this_week_count,
        success_count=success_count,
        error_count=error_count,
    )


# --- ステップ1: テーマ入力 → タイトル案 + SEOチェック -----------------------

@app.route("/generate", methods=["GET"])
def generate_form():
    return render_template("generate.html", active="generate", categories=get_categories())


@app.route("/generate", methods=["POST"])
def generate_titles_submit():
    topic = request.form.get("topic", "").strip()
    category = request.form.get("category", "").strip()
    include_amazon = request.form.get("include_amazon") == "on"
    include_image = request.form.get("include_image") == "on"

    def task(log):
        categories = ga.fetch_categories()
        log("タイトル案を生成しています...")
        candidates = ga.generate_title_candidates(categories, topic=topic or None, log=log)

        log("既存記事タイトルを取得し、SEOルールでチェックしています...")
        try:
            existing_titles = ga.fetch_existing_titles()
        except Exception:
            existing_titles = []
        seo_results = ga.check_titles_seo(candidates, existing_titles, log=log)

        seo_map = {r.get("title"): r for r in seo_results}
        merged = []
        for c in candidates:
            r = seo_map.get(c.get("title"), {})
            merged.append({
                "title": c.get("title", ""),
                "target_keyword": c.get("target_keyword", ""),
                "verdict": r.get("verdict", "ok"),
                "reasons": r.get("reasons", []),
            })

        return {
            "topic": topic,
            "category": category,
            "include_amazon": include_amazon,
            "include_image": include_image,
            "candidates": merged,
        }

    job_id = start_job(task)
    return redirect(url_for("titles_status", job_id=job_id))


@app.route("/generate/titles/<job_id>")
def titles_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        abort(404)
    return render_template("titles.html", active="generate", job_id=job_id, job=job)


# --- ステップ2: タイトルを選択 → アウトライン生成 ---------------------------

@app.route("/generate/select-title", methods=["POST"])
def select_title():
    titles_job_id = request.form.get("job_id", "")
    index = int(request.form.get("index", "-1"))

    titles_job = get_job(titles_job_id)
    if titles_job is None or titles_job.get("status") != "done":
        abort(400)
    data = titles_job["result"]
    if index < 0 or index >= len(data["candidates"]):
        abort(400)
    chosen = data["candidates"][index]

    def task(log):
        categories = ga.fetch_categories()
        log(f"「{chosen['title']}」の構成案を作成しています...")
        outline = ga.generate_outline(
            chosen["title"], chosen.get("target_keyword", ""), categories,
            category=data.get("category") or None, log=log,
        )
        return {
            "outline": outline,
            "include_amazon": data["include_amazon"],
            "include_image": data["include_image"],
        }

    job_id = start_job(task)
    return redirect(url_for("outline_status", job_id=job_id))


@app.route("/generate/outline/<job_id>")
def outline_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        abort(404)
    return render_template("outline.html", active="generate", job_id=job_id, job=job)


# --- ステップ3: アウトラインを承認 → 本文生成・WordPress下書き投稿 ----------

@app.route("/generate/approve-outline", methods=["POST"])
def approve_outline():
    outline_job_id = request.form.get("job_id", "")
    outline_job = get_job(outline_job_id)
    if outline_job is None or outline_job.get("status") != "done":
        abort(400)
    data = outline_job["result"]

    def task(log):
        result = ga.run_pipeline_from_outline(
            data["outline"],
            include_amazon=data["include_amazon"],
            include_featured_image=data["include_image"],
            log=log,
        )
        entry = {
            "id": outline_job_id,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": result["title"],
            "seo_title": result["seo_title"],
            "meta_description": result["meta_description"],
            "keywords": result["keywords"],
            "category": result["category"],
            "char_count": result["char_count"],
            "wp_link": result["wp_link"],
            "local_path": result["local_path"],
            "error": result["error"],
        }
        save_history_entry(entry)
        return entry

    job_id = start_job(task)
    return redirect(url_for("job_status", job_id=job_id))


@app.route("/history")
def history_page():
    return render_template("history.html", active="history", history=load_history())


# --- リライト: 既存記事を選ぶ → リライト → プレビュー → 上書き保存 ---------

@app.route("/rewrite")
def rewrite_list():
    try:
        posts = ga.fetch_posts_for_rewrite()
    except Exception as exc:
        posts = []
        return render_template("rewrite.html", active="rewrite", posts=posts, categories=get_categories(), error=str(exc))
    return render_template("rewrite.html", active="rewrite", posts=posts, categories=get_categories(), error=None)


@app.route("/rewrite/start", methods=["POST"])
def rewrite_start():
    post_id = int(request.form.get("post_id", "0"))
    category = request.form.get("category", "").strip()
    include_amazon = request.form.get("include_amazon") == "on"
    include_image = request.form.get("include_image") == "on"

    def task(log):
        categories = ga.fetch_categories()
        existing = ga.fetch_post_for_rewrite(post_id)
        log(f"「{existing['title']}」をリライトしています...")
        article = ga.generate_rewrite(
            existing["title"], existing["text"], categories, category=category or None, log=log
        )
        return {
            "post_id": post_id,
            "original_title": existing["title"],
            "article": article,
            "include_amazon": include_amazon,
            "include_image": include_image,
        }

    job_id = start_job(task)
    return redirect(url_for("rewrite_preview", job_id=job_id))


@app.route("/rewrite/preview/<job_id>")
def rewrite_preview(job_id: str):
    job = get_job(job_id)
    if job is None:
        abort(404)
    return render_template("rewrite_preview.html", active="rewrite", job_id=job_id, job=job)


@app.route("/rewrite/confirm", methods=["POST"])
def rewrite_confirm():
    preview_job_id = request.form.get("job_id", "")
    preview_job = get_job(preview_job_id)
    if preview_job is None or preview_job.get("status") != "done":
        abort(400)
    data = preview_job["result"]

    def task(log):
        categories = ga.fetch_categories()
        result = ga.finalize_and_publish(
            data["article"],
            categories,
            include_amazon=data["include_amazon"],
            include_featured_image=data["include_image"],
            rewrite_post_id=data["post_id"],
            log=log,
        )
        entry = {
            "id": preview_job_id,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": result["title"],
            "seo_title": result["seo_title"],
            "meta_description": result["meta_description"],
            "keywords": result["keywords"],
            "category": result["category"],
            "char_count": result["char_count"],
            "wp_link": result["wp_link"],
            "local_path": result["local_path"],
            "error": result["error"],
        }
        save_history_entry(entry)
        return entry

    job_id = start_job(task)
    return redirect(url_for("job_status", job_id=job_id))


@app.route("/status")
def status_page():
    services = []

    services.append({
        "name": "Anthropic (Claude)",
        "status": "ok" if os.environ.get("ANTHROPIC_API_KEY") else "warn",
        "detail": "設定済み" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEYが未設定です",
    })

    amazon_keys = ["AMAZON_ACCESS_KEY", "AMAZON_SECRET_KEY", "AMAZON_ASSOCIATE_TAG"]
    if all(os.environ.get(k) for k in amazon_keys):
        services.append({
            "name": "Amazon Creators API",
            "status": "ok",
            "detail": "設定済み(アソシエイト資格審査等でAmazon側から拒否される場合、記事生成時に検索リンクへ自動フォールバックします)",
        })
    else:
        services.append({
            "name": "Amazon Creators API",
            "status": "warn",
            "detail": "未設定です(なくても検索リンク形式で自動フォールバックします)",
        })

    if os.environ.get("WP_URL") and os.environ.get("WP_USERNAME") and os.environ.get("WP_APP_PASSWORD"):
        try:
            ga.fetch_categories()
            services.append({"name": "WordPress", "status": "ok", "detail": os.environ.get("WP_URL", "")})
        except Exception as exc:
            services.append({"name": "WordPress", "status": "ng", "detail": str(exc)})
    else:
        services.append({"name": "WordPress", "status": "warn", "detail": "WP_URL / WP_USERNAME / WP_APP_PASSWORDが未設定です"})

    services.append({
        "name": "Unsplash(アイキャッチ画像)",
        "status": "ok" if os.environ.get("UNSPLASH_ACCESS_KEY") else "warn",
        "detail": "設定済み" if os.environ.get("UNSPLASH_ACCESS_KEY") else "未設定です(なくてもアイキャッチなしで生成されます)",
    })

    return render_template("status.html", active="status", services=services)


@app.route("/jobs/<job_id>")
def job_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        return redirect(url_for("dashboard"))
    return render_template("job.html", active="generate", job_id=job_id, job=job)


@app.route("/jobs/<job_id>/data")
def job_status_data(job_id: str):
    job = get_job(job_id) or {}
    return {
        "status": job.get("status", "unknown"),
        "logs": job.get("logs", []),
        "result": job.get("result"),
        "error": job.get("error"),
    }


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

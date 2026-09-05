"""おもちゃブログ向けの記事を生成し、WordPressに下書きとして投稿するスクリプト。
サイト(omotya-museum.com)の既存の記事スタイル(「ゆう」の会話導入、【】タイトル、
Cocoonテーマのふきだし/マーカー、Amazon購入ボタン)に合わせて生成する。
公開は行わず、下書き(draft)として保存するので、最終確認は手動で行うこと。

必要な環境変数(.envファイルに記載):
  ANTHROPIC_API_KEY     Anthropic APIキー
  AMAZON_ACCESS_KEY     Amazon Creators APIの認証情報ID(amzn1.application-oa2-client...)
  AMAZON_SECRET_KEY     Amazon Creators APIのクライアントシークレット
  AMAZON_ASSOCIATE_TAG  Amazonアソシエイトタグ
  WP_URL                WordPressサイトのURL(例: https://www.omotya-museum.com)
  WP_USERNAME           WordPressユーザー名
  WP_APP_PASSWORD       WordPressのアプリケーションパスワード
  ANTHROPIC_MODEL       (任意) 使用するモデルID。省略時は claude-sonnet-5

必要なライブラリ: requirements.txt を参照 (pip install -r scripts/requirements.txt)

実行方法:
  python scripts/generate_article.py
"""

import json
import os
import re
import sys
import urllib.parse
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"

# サイトに既存の「ゆう」アイコン画像(会話ブロックで使用)
YU_AVATAR_URL = (
    "https://www.omotya-museum.com/wp-content/uploads/2024/09/"
    "cropped-e5d5fac4-7a55-4700-b17e-9be062151c69.webp"
)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_categories() -> list:
    """サイトに既存のカテゴリー一覧を取得する。"""
    wp_url = os.environ["WP_URL"].rstrip("/")
    response = requests.get(
        f"{wp_url}/wp-json/wp/v2/categories",
        params={"per_page": 100},
        headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def build_article_prompt(categories: list) -> str:
    category_names = "、".join(c["name"] for c in categories if c["name"] != "Uncategorized")

    return f"""あなたは「おもちゃミュージアム」というブログの専属ライターです。
このブログには「ゆう」というハムスターのキャラクターがいて、読者からの悩み相談に答える形で
記事を書き始めるのが定番のスタイルです。おもちゃに関するブログ記事を1本作成してください。
テーマは自由に決めてください(新作トイのレビュー、レトロトイの魅力、知育玩具の選び方、DIYおもちゃ、
コレクター向け情報、プレゼント選びのコツ、旅行先での子ども向けお土産など、おもちゃに関する範囲内で、
毎回異なる話題にしてください)。

## タイトル
【】で始まる、具体的で読者の悩みに刺さるフックタイトルにする。
例:「【福岡空港お土産】出張パパ必見!子どもが喜ぶおもちゃまとめ」「【年齢別】知育玩具の選び方完全ガイド」

## 冒頭の会話導入
記事の最初に、悩みを持つ読者と「ゆう」が掛け合いをする設定を作る。
- reader_persona: 読者役の短いラベル(例:「読者(プレゼントに悩むママ)」「読者(出張中のパパ)」)
- reader_question: その読者の悩み・質問(1〜2文)
- yu_answer: 「ゆう」の返答(1〜2文、絵文字を使ってよい、親しみやすいトーン)

## 文体・トーン(本文)
- 「です・ます調」で、丁寧で優しい雰囲気にする
- 具体的で実用的な情報(店舗名、商品の特徴など)を盛り込む
- SEOを意識し、検索されやすいキーワードを自然に本文へ盛り込む
- アフィリエイト記事として成立するよう、紹介する商品への興味を高める文章にする

## 文字数・構成
- 本文(content)は5000文字以上
- <h2>から始めること(タイトルや冒頭の会話は含めない。それらは別途組み立てるため)
- 複数の見出し(h2/h3)によるセクション → まとめ、という構成にする

## 装飾(デザイン)
本文のHTML内で、以下のような装飾を適宜使ってください(インラインstyleで指定すること。WordPressの投稿にそのまま貼り付けるため、外部CSSには依存しないこと):
- 重要な語句は <strong> で太字にする
- 特に注目してほしい語句は <span class="marker-under">のように囲む(このサイトの既存記事で使われているマーカースタイル)
- 「ポイント」「まとめ」などは背景色付きのボックスにする。例:
  <div style="background:#fff3cd;border-left:4px solid #ffc107;padding:16px;margin:16px 0;border-radius:4px;"><strong>ポイント</strong><br>ここに内容</div>
- 比較や一覧が適切な場面ではtable要素も使ってよい

## カテゴリー
このブログの既存カテゴリーの中から、記事に最も合うものを1つだけ選んでください(新しいカテゴリー名を作らないこと):
{category_names}

## 出力形式
必ず次のJSON形式のみで返してください。JSON以外の文章やコードブロックの記号は含めないでください。

{{
  "title": "【】から始まる記事タイトル(検索されやすいキーワードを含む)",
  "meta_description": "検索結果に表示される説明文(120文字程度)",
  "keywords": ["SEOキーワード1", "SEOキーワード2", "SEOキーワード3"],
  "category": "上のカテゴリー一覧から選んだ1つ",
  "reader_persona": "読者役の短いラベル",
  "reader_question": "読者の悩み・質問",
  "yu_answer": "ゆうの返答",
  "amazon_search_keyword": "記事に関連する商品をAmazonで探すための検索キーワード(具体的な商品カテゴリ名、日本語)",
  "content": "h2から始まる本文HTML(5000文字以上)"
}}
"""


def generate_article(categories: list) -> dict:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    response = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": build_article_prompt(categories)}],
        },
        timeout=120,
    )
    response.raise_for_status()
    content_blocks = response.json()["content"]
    text_block = next(
        (block for block in content_blocks if block.get("type") == "text"), None
    )
    if text_block is None:
        raise ValueError(f"テキスト形式のレスポンスが見つかりませんでした: {content_blocks}")
    text = text_block["text"].strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"JSON形式のレスポンスを取得できませんでした: {text}")

    return json.loads(match.group(0))


def build_intro_balloon_html(persona: str, question: str, answer: str) -> str:
    """「ゆう」との会話導入(Cocoonのふきだしブロック相当)を組み立てる。"""
    return f"""<div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin:16px 0;background:#fafafa;">
<p style="margin:0 0 12px;"><strong>{persona}</strong><br>{question}</p>
<div style="display:flex;align-items:flex-start;gap:12px;">
<img src="{YU_AVATAR_URL}" alt="ゆう" style="width:56px;height:56px;border-radius:50%;object-fit:cover;flex-shrink:0;">
<div style="background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 14px;">
<strong>ゆう</strong><br>{answer}
</div>
</div>
</div>"""


def search_amazon_products(keyword: str, item_count: int = 3) -> list:
    from amazon_creatorsapi import AmazonCreatorsApi

    # バージョン"3.3"はamazon.co.jp(日本)向けのCreators API認証エンドポイントを指す。
    amazon = AmazonCreatorsApi(
        os.environ["AMAZON_ACCESS_KEY"],
        os.environ["AMAZON_SECRET_KEY"],
        "3.3",
        os.environ["AMAZON_ASSOCIATE_TAG"],
        country="JP",
    )
    result = amazon.search_items(keywords=keyword, item_count=item_count)
    return result.items or []


def build_product_card_html(products: list) -> str:
    """Amazon Creators APIから実際の商品情報が取れた場合のカード表示。"""
    blocks = []
    for product in products:
        try:
            title_text = product.item_info.title.display_value
            url = product.detail_page_url
            image_url = product.images.primary.large.url
        except AttributeError:
            continue

        blocks.append(f"""<div style="border:1px solid #ddd;border-radius:12px;padding:16px;margin:16px 0;display:flex;gap:16px;flex-wrap:wrap;">
<img src="{image_url}" alt="{title_text}" style="width:140px;height:140px;object-fit:contain;flex-shrink:0;">
<div style="flex:1;min-width:200px;">
<p style="font-weight:bold;margin:0 0 12px;">{title_text}</p>
<a rel="nofollow noopener sponsored" href="{url}" target="_blank" style="display:inline-block;background:#ff6600;color:#fff;font-weight:700;padding:10px 20px;border-radius:8px;text-decoration:none;">▶ Amazonで見る</a>
</div>
</div>""")
    return "\n".join(blocks)


def build_amazon_search_button_html(keyword: str) -> str:
    """Creators APIが使えない場合のフォールバック(検索結果への通常アフィリエイトリンク)。"""
    tag = os.environ.get("AMAZON_ASSOCIATE_TAG", "")
    query = urllib.parse.quote(keyword)
    url = f"https://www.amazon.co.jp/s?k={query}"
    if tag:
        url += f"&tag={urllib.parse.quote(tag)}"

    return f"""<div style="margin:16px 0;">
<a rel="nofollow noopener sponsored" href="{url}" target="_blank" style="display:inline-block;background:#ff6600;color:#fff;font-weight:700;padding:10px 20px;border-radius:8px;text-decoration:none;">▶ Amazonで「{keyword}」を探す</a>
</div>"""


def resolve_category_id(category_name: str, categories: list) -> int | None:
    for c in categories:
        if c["name"] == category_name:
            return c["id"]
    return None


def post_to_wordpress_draft(title: str, content: str, category_id: int | None) -> str:
    wp_url = os.environ["WP_URL"].rstrip("/")
    username = os.environ["WP_USERNAME"]
    app_password = os.environ["WP_APP_PASSWORD"]

    payload = {"title": title, "content": content, "status": "draft"}
    if category_id is not None:
        payload["categories"] = [category_id]

    response = requests.post(
        f"{wp_url}/wp-json/wp/v2/posts",
        auth=(username, app_password),
        headers={"User-Agent": BROWSER_USER_AGENT},
        json=payload,
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"WordPressへの下書き保存に失敗しました (HTTP {response.status_code}): {response.text}"
        )
    post = response.json()
    return post.get("link") or f"post id {post.get('id')}"


def save_article_locally(article: dict, full_content: str) -> str:
    os.makedirs("articles", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^\w\-]", "", article["title"].replace(" ", "-"))[:30] or "article"
    filepath = os.path.join("articles", f"{timestamp}-{slug}.html")

    keywords_line = ", ".join(article.get("keywords", []))
    html = f"""<!-- title: {article['title']} -->
<!-- meta description: {article.get('meta_description', '')} -->
<!-- keywords: {keywords_line} -->
<!-- category: {article.get('category', '')} -->

{full_content}"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


def main() -> None:
    categories = fetch_categories()
    article = generate_article(categories)
    print(f"生成された記事タイトル: {article['title']}")

    intro_html = build_intro_balloon_html(
        article.get("reader_persona", "読者"),
        article.get("reader_question", ""),
        article.get("yu_answer", ""),
    )

    product_html = ""
    amazon_keyword = article.get("amazon_search_keyword")
    if amazon_keyword:
        try:
            products = search_amazon_products(amazon_keyword)
            product_html = build_product_card_html(products)
        except Exception as exc:
            print(
                f"Amazon商品検索に失敗しました(検索リンクにフォールバックします): {exc}",
                file=sys.stderr,
            )
        if not product_html:
            product_html = build_amazon_search_button_html(amazon_keyword)

    full_content = (
        f"{intro_html}\n\n{article['content']}\n\n"
        f"<h2>おすすめ商品</h2>\n{product_html}"
    )

    filepath = save_article_locally(article, full_content)
    print(f"記事をローカルに保存しました: {filepath}")

    try:
        category_id = resolve_category_id(article.get("category", ""), categories)
        link = post_to_wordpress_draft(article["title"], full_content, category_id)
        print(f"WordPressに下書き保存しました: {link}")
    except Exception as exc:
        print(f"WordPressへの下書き保存に失敗しました(ローカルfiles保存のみ完了): {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)

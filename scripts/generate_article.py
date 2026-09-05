"""おもちゃブログ向けの記事を生成し、articles/ 以下にHTMLファイルとして保存するスクリプト。
WordPressへの自動投稿は行わない。生成された記事は手動でWordPressに貼り付けて公開する。

必要な環境変数(.envファイルに記載):
  ANTHROPIC_API_KEY     Anthropic APIキー
  AMAZON_ACCESS_KEY     Amazon PA-APIのAccess Key ID
  AMAZON_SECRET_KEY     Amazon PA-APIのSecret Key
  AMAZON_ASSOCIATE_TAG  Amazonアソシエイトタグ
  ANTHROPIC_MODEL       (任意) 使用するモデルID。省略時は claude-sonnet-5

必要なライブラリ: requirements.txt を参照 (pip install -r scripts/requirements.txt)

実行方法:
  python scripts/generate_article.py
"""

import json
import os
import re
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"

ARTICLE_PROMPT = """あなたは経験豊富なプロブロガー兼SEOライターです。おもちゃ専門ブログの記事を1本作成してください。
テーマは自由に決めてください(新作トイのレビュー、レトロトイの魅力、知育玩具の選び方、DIYおもちゃ、
コレクター向け情報、プレゼント選びのコツなど、おもちゃに関する範囲内で、毎回異なる話題にしてください)。

## 文体・トーン
- 「です・ます調」で、丁寧で優しい雰囲気にする
- 読者の悩みへの共感 → 具体的な解決策・情報提供 → まとめ、という一般的なプロブロガーの構成を意識する
- SEOを意識し、検索されやすいキーワードを自然に本文へ盛り込む
- アフィリエイト記事として成立するよう、紹介する商品への興味を高める文章にする

## 文字数・構成
- 本文(content)は5000文字以上
- 導入文 → 複数の見出し(h2/h3)によるセクション → まとめ、という構成にする

## 装飾(デザイン)
本文のHTML内で、以下のような装飾を適宜使ってください(インラインstyleで指定すること。WordPressの投稿にそのまま貼り付けるため、外部CSSには依存しないこと):
- 重要な語句は <strong> で太字にする
- 特に注目してほしい箇所は <span style="color:#e63946;font-weight:bold;">のように強調色をつける
- 「ポイント」「まとめ」などは背景色付きのボックスにする。例:
  <div style="background:#fff3cd;border-left:4px solid #ffc107;padding:16px;margin:16px 0;border-radius:4px;"><strong>ポイント</strong><br>ここに内容</div>
- 比較や一覧が適切な場面ではtable要素も使ってよい

## 出力形式
必ず次のJSON形式のみで返してください。JSON以外の文章やコードブロックの記号は含めないでください。

{
  "title": "記事タイトル(検索されやすいキーワードを含む、32文字程度)",
  "meta_description": "検索結果に表示される説明文(120文字程度)",
  "keywords": ["SEOキーワード1", "SEOキーワード2", "SEOキーワード3"],
  "category": "この記事に最も合うWordPressのカテゴリー名(自由に決めてよい)",
  "amazon_search_keyword": "記事に関連する商品をAmazonで探すための検索キーワード(具体的な商品カテゴリ名、日本語)",
  "content": "上記の文体・構成・装飾を反映したHTML本文(5000文字以上)"
}
"""


def generate_article() -> dict:
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
            "messages": [{"role": "user", "content": ARTICLE_PROMPT}],
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


def search_amazon_products(keyword: str, item_count: int = 3) -> list:
    from amazon_paapi import AmazonApi

    amazon = AmazonApi(
        os.environ["AMAZON_ACCESS_KEY"],
        os.environ["AMAZON_SECRET_KEY"],
        os.environ["AMAZON_ASSOCIATE_TAG"],
        "JP",
    )
    result = amazon.search_items(keywords=keyword, item_count=item_count)
    return result.items or []


def build_product_html(products: list) -> str:
    blocks = []
    for product in products:
        try:
            title_text = product.item_info.title.display_value
            url = product.detail_page_url
            image_url = product.images.primary.large.url
        except AttributeError:
            continue

        blocks.append(
            '<div class="amazon-product">'
            f'<a href="{url}" target="_blank" rel="nofollow noopener sponsored">'
            f'<img src="{image_url}" alt="{title_text}">'
            f"<p>{title_text}</p>"
            "</a></div>"
        )
    return "\n".join(blocks)


def save_article(article: dict, product_html: str) -> str:
    os.makedirs("articles", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^\w\-]", "", article["title"].replace(" ", "-"))[:30] or "article"
    filepath = os.path.join("articles", f"{timestamp}-{slug}.html")

    keywords_line = ", ".join(article.get("keywords", []))
    product_section = (
        f"<h2>おすすめ商品</h2>\n{product_html}\n" if product_html else ""
    )

    html = f"""<!-- title: {article['title']} -->
<!-- meta description: {article.get('meta_description', '')} -->
<!-- keywords: {keywords_line} -->
<!-- category: {article.get('category', '')} -->

<h1>{article['title']}</h1>
{article['content']}
{product_section}"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


def main() -> None:
    article = generate_article()
    print(f"生成された記事タイトル: {article['title']}")

    product_html = ""
    amazon_keyword = article.get("amazon_search_keyword")
    if amazon_keyword:
        try:
            products = search_amazon_products(amazon_keyword)
            product_html = build_product_html(products)
        except Exception as exc:
            print(
                f"Amazon商品検索に失敗しました(商品情報なしで記事を保存します): {exc}",
                file=sys.stderr,
            )

    filepath = save_article(article, product_html)
    print(f"記事を保存しました: {filepath}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"エラーが発生しました: {exc}", file=sys.stderr)
        sys.exit(1)

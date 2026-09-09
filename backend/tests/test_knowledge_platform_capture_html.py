from __future__ import annotations

import pytest

from knowledge_platform.capture.html import canonicalize_url, extract_article


def test_canonicalize_url_removes_tracking_sorts_query_and_fragment() -> None:
    assert canonicalize_url(
        "HTTPS://例子.测试:443//story?utm_source=mail&b=2&gclid=x&a=1#comments"
    ) == "https://xn--fsqu00a.xn--0zwm56d/story?a=1&b=2"


def test_canonicalize_url_rejects_non_http_credentials_and_long_urls() -> None:
    with pytest.raises(ValueError):
        canonicalize_url("file:///tmp/article.html")
    with pytest.raises(ValueError):
        canonicalize_url("https://user:password@example.com/article")
    with pytest.raises(ValueError):
        canonicalize_url("https://example.com/" + "x" * 1900)


def test_canonicalize_url_preserves_non_default_ports_and_brackets_ipv6() -> None:
    assert canonicalize_url("HTTP://[2001:0DB8::1]:8080//article#part") == "http://[2001:db8::1]:8080/article"
    assert canonicalize_url("https://127.0.0.1:9443/article") == "https://127.0.0.1:9443/article"
    with pytest.raises(ValueError, match="control"):
        canonicalize_url("https://example.com/article\nX-Injected: yes")


def test_extracts_article_noise_title_and_lazy_relative_image() -> None:
    html = """
    <html><head>
      <title>Fallback title</title>
      <meta property="og:title" content="A real article">
      <meta name="author" content="Author A">
      <meta property="og:image" content="/cover.jpg">
    </head><body>
      <header>Site navigation</header><main class="article-content">
        <h1>A real article</h1><p>First paragraph.</p>
        <div class="advertisement">Buy this</div>
        <figure><img data-src="images/body.png"><figcaption>Diagram</figcaption></figure>
        <p><a href="/next">Read next</a></p>
        <script>secret_noise()</script>
      </main><footer>Footer noise</footer>
    </body></html>
    """
    metadata, markdown = extract_article(html, "https://example.com/section/page")
    assert metadata == {
        "title": "A real article",
        "author": "Author A",
        "site_name": "",
        "description": "",
        "image_url": "https://example.com/cover.jpg",
    }
    assert "First paragraph." in markdown
    assert "Buy this" not in markdown and "secret_noise" not in markdown and "Footer noise" not in markdown
    assert "![Diagram](https://example.com/section/images/body.png)" in markdown
    assert "[Read next](https://example.com/next)" in markdown


def test_wechat_body_uses_js_content_removes_hidden_and_preserves_heading() -> None:
    html = """
    <html><head><meta property="og:title" content="微信文章"></head><body>
      <div id="js_name">公众号作者</div>
      <div id="js_content">
        <section><strong>一、背景</strong><p>正文内容。</p></section>
        <section style="display: none">隐藏推广</section>
        <section><p>结语</p><img src="/last.png"></section>
      </div>
      <div class="related-posts">推荐阅读</div>
    </body></html>
    """
    metadata, markdown = extract_article(html, "https://mp.weixin.qq.com/s/example")
    assert metadata["author"] == "公众号作者"
    assert metadata["site_name"] == "微信公众平台"
    assert "背景" in markdown and "正文内容。" in markdown and "结语" in markdown
    assert "隐藏推广" not in markdown and "推荐阅读" not in markdown
    assert "https://mp.weixin.qq.com/last.png" in markdown

"""Report quality decides the grade. A finished pipeline is not an A."""

from evaluation.rubric import score_task


GOAL = "帮我调研一下什么是jev，搜索资料，输出报告"

POOR_HTML = """
<html><body>
<p class='kicker'>AOP Report</p>
<h1>帮我调研一下什么是jev，搜索资料，输出报告</h1>
<nav><h2>Report Plan</h2></nav>
<section><h2>标题</h2><p>帮我调研一下什么是jev，搜索资料，输出报告</p></section>
<section><h2>研究发现</h2><p>Jev 不会聊天。</p><figcaption>研究发现</figcaption>
<p>上游结果中没有可绘制的数值。</p></section>
<section><h2>研究发现</h2><p>再次重复研究发现。</p></section>
<section><h2>分析</h2><p>评价分化。</p></section>
<section><h2>结论</h2><p>仍需核实。</p></section>
<section><h2>摘要</h2><p>两种含义。</p></section>
<p>[1] 知乎；[2] 腾讯新闻</p>
</body></html>
"""

GOOD_HTML = """
<html><head><style>.sheet{padding:24px}</style></head><body>
<article class="sheet">
<h1>Jev 的两种含义</h1>
<p class="dek">Jev 既指一个非生成模型，也指一份细胞外囊泡期刊。</p>
<nav class="toc"><a href="#findings"><span>3</span>研究发现</a></nav>
<section class="block"><h2>摘要</h2><p class="lead">Jev 目前主要有两种含义，一种是模型，一种是期刊。</p></section>
<section class="block"><h2>背景</h2><p>TypeSafe AI 近期发布了一个不会自由生成文本的模型。</p></section>
<section class="block"><h2>研究发现</h2><p>它只在有限选项里做选择，计数和超大分类都弱于常见语言模型。</p></section>
<section class="block"><h2>分析</h2><p>公开评价分化，官方技术文档仍不完整，不能把热度当成能力。</p></section>
<section class="block"><h2>结论</h2><p>中文网络里的热度来自模型，学术缩写则指向 Journal of Extracellular Vesicles。</p></section>
<ol class="refs">
<li><a href="https://example.com/jev">https://example.com/jev</a></li>
<li><a href="https://onlinelibrary.wiley.com/journal/jev">https://onlinelibrary.wiley.com/journal/jev</a></li>
</ol>
</article></body></html>
"""


def _task(status="completed"):
    return {"status": status, "created_at": None, "finished_at": None}


def _nodes(html: str | None):
    if html is None:
        return [{"node_key": "hop-a", "agent_key": "claude-code", "status": "success", "output_json": {}}]
    return [
        {"node_key": "hop-a", "agent_key": "claude-code", "status": "success", "output_json": {}},
        {
            "node_key": "hop-d",
            "agent_key": "hermes",
            "status": "success",
            "output_json": {"data": {"query": GOAL, "files": {"report.html": html}}},
        },
    ]


def test_poor_report_cannot_score_a():
    scored = score_task(_task(), _nodes(POOR_HTML))
    assert scored["grade"] in {"C", "D", "F"}
    assert scored["score"] < 75
    assert scored["dimensions"]["layout"] < 40
    assert scored["dimensions"]["prose"] < 50


def test_structured_report_outranks_a_bare_completion():
    good = score_task(_task(), _nodes(GOOD_HTML))
    bare = score_task(_task(), _nodes(None))
    assert good["grade"] in {"A", "B"}
    assert good["score"] > bare["score"]
    assert bare["grade"] != "A"
    assert "行文" in good["summary"] or "引用" in good["summary"] or "版式" in good["summary"] or "结构" in good["summary"] or "执行" in good["summary"]
